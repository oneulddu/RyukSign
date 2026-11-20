//
//  Server.swift
//  feather
//
//  Created by samara on 22.08.2024.
//  Copyright © 2024 Lakr Aream. All Rights Reserved.
//  ORIGINALLY LICENSED UNDER GPL-3.0, MODIFIED FOR USE FOR FEATHER
//

import Foundation
import Vapor
import NIOSSL
import NIOTLS
import SwiftUI
import IDeviceSwift

// Shared by Vapor event loops and the detached install watcher.
final class PayloadReads: @unchecked Sendable {
	private let lock = NSLock()
	private var _readers = Set<String>()
	private var _generation = 0
	private var _idleSince: Date?
	private var _closed = false

	var idleWindow: (generation: Int, since: Date)? {
		lock.withLock {
			guard !_closed, _readers.isEmpty else { return nil }
			return (_generation, _idleSince ?? Date())
		}
	}

	// Choose between admitting a resumed GET and committing the install verdict
	// under one lock: a snapshot alone leaves a race before stopping the server.
	func claimInstallVerdict(generation: Int) -> Bool {
		lock.withLock {
			guard !_closed, _readers.isEmpty, _generation == generation else { return false }
			_closed = true
			return true
		}
	}

	fileprivate func begin(_ requestID: String) -> Bool {
		lock.withLock {
			guard !_closed else { return false }
			_generation += 1
			_readers.insert(requestID)
			return true
		}
	}

	fileprivate func finish(_ requestID: String) -> Bool {
		lock.withLock {
			// A failed HTTP/1 body can report completion more than once.
			guard _readers.remove(requestID) != nil, _readers.isEmpty else { return false }
			_idleSince = Date()
			return true
		}
	}

	fileprivate func close() { lock.withLock { _closed = true } }
}

// MARK: - Class
class ServerInstaller: Identifiable, ObservableObject {
	let id = UUID()
	let port = Int.random(in: 4000...8000)
	let payloadReads = PayloadReads()
	private var _needsShutdown = false
	private var _acceptsUpdates = true
	private let _packageLock = NSLock()
	private var _package: InstallationArchive?
	var package: InstallationArchive? {
		get {
			_packageLock.lock()
			defer { _packageLock.unlock() }
			return _package
		}
		set {
			_packageLock.lock()
			defer { _packageLock.unlock() }
			_package = newValue
		}
	}
	var manifestUrl: URL?
	private(set) var startupError: Error?
	// HTTP callbacks use values captured on the view context queue.
	let app: (identifier: String?, name: String?, version: String?)
	@ObservedObject var viewModel: InstallerStatusViewModel
	private var _server: Application?

	private var backgroundTaskManager: BackgroundTaskManager?

	@MainActor
	init(app: AppInfoPresentable, viewModel: InstallerStatusViewModel) throws {
		self.app = (app.identifier, app.name, app.version)
		self.viewModel = viewModel

		let mode = getServerMethod() == 1 ? "semi-local" : "fully-local"
		FileLogger.log("installer starting: mode=\(mode) host=\(sni()) port=\(port) ipFix=\(getIPFix())", category: "install")

		do {
			let server = try setupApp(port: port)
			_server = server
			try _configureRoutes()
			try server.server.start()
			_needsShutdown = true
			FileLogger.log("server listening on \(sni()):\(port), payload=\(payloadEndpoint.absoluteString)", category: "install")
		} catch {
			FileLogger.error("server failed to start: \(error)", category: "install")
			startupError = error
		}
	}
	
	deinit {
		_shutdownServer()
		backgroundTaskManager?.stop()
	}
	
	private func _configureRoutes() throws {
		_server?.get("*") { [weak self] req in
			guard let self else { return Response(status: .badGateway) }

			FileLogger.log("request: \(req.method.rawValue) \(req.url.path)", category: "install")

			switch req.url.path {
			case plistEndpoint.path:
				if req.method == .GET { self._updateStatus(.sendingManifest) }
				return Response(status: .ok, version: req.version, headers: [
					"Content-Type": "text/xml",
				], body: .init(data: installManifestData))
			case displayImageSmallEndpoint.path:
				return Response(status: .ok, version: req.version, headers: [
					"Content-Type": "image/png",
				], body: .init(data: displayImageSmallData))
			case displayImageLargeEndpoint.path:
				return Response(status: .ok, version: req.version, headers: [
					"Content-Type": "image/png",
				], body: .init(data: displayImageLargeData))
			case payloadEndpoint.path:
				guard let package = package else {
					FileLogger.error("payload unavailable request=\(req.method)", category: "install")
					return Response(status: .notFound)
				}

				// Vapor routes HEAD through GET, but suppresses the response body. It must
				// never start a transfer, finish one, or change the install state.
				guard req.method == .GET else {
					let response = req.fileio.streamFile(at: package.url.path, mediaType: .binary) { [package] _ in
						withExtendedLifetime(package) {}
					}
					response.headers.responseCompression = .disable
					FileLogger.log("payload probe HTTP \(response.status.code) bytes=\(response.headers.first(name: .contentLength) ?? "unknown")", category: "install")
					return response
				}

				let requestID = UUID().uuidString
				FileLogger.log("payload GET id=\(requestID) range=\(req.headers.first(name: .range) ?? "full")", category: "install")
				let response = req.fileio.streamFile(at: package.url.path, mediaType: .binary) { [weak self, package] result in
					defer { withExtendedLifetime(package) {} }
					guard let self else { return }
					switch result {
					case .success:
						FileLogger.log("payload stream completed id=\(requestID)", category: "install")
					case .failure(let error):
						// iOS drops and resumes long downloads, so the install watch decides the outcome.
						FileLogger.error("payload stream ended early id=\(requestID): \(error)", category: "install")
					}
					if self.payloadReads.finish(requestID) { self._updateStatus(.installing) }
				}
				// IPAs are already ZIPs. Compressing HEAD's empty body advertises the
				// compressed empty size instead of the IPA size, breaking iOS preflight.
				response.headers.responseCompression = .disable
				FileLogger.log("payload response id=\(requestID) HTTP \(response.status.code) bytes=\(response.headers.first(name: .contentLength) ?? "unknown")", category: "install")
				if response.status == .ok || response.status == .partialContent {
					guard self.payloadReads.begin(requestID) else { return Response(status: .notFound) }
					self._updateStatus(.sendingPayload)
				} else if response.status == .notModified {
					// Cached bytes are available, but Vapor will not call stream completion.
					self._updateStatus(.installing)
				} else {
					self._updateStatus(.broken(Abort(response.status, reason: "Could not serve the signed IPA.")))
				}
				return response
			case "/healthz":
				return Response(status: .ok)
			case "/install":
				var headers = HTTPHeaders()
				headers.add(name: .contentType, value: "text/html")
				return Response(status: .ok, headers: headers, body: .init(string: self.html))
			default:
				return Response(status: .notFound)
			}
		}
	}
	
	// installd fails silently when unreachable, so prove it first — but not for Semi Local, where probing a LAN address would prompt for local network access.
	func selfCheck() async -> Error? {
		guard getServerMethod() != 1 else { return nil }

		let host = sni()
		let addresses = Self.resolve(host)
		FileLogger.log("\(host) resolves to \(addresses.isEmpty ? "nothing" : addresses.joined(separator: ", "))", category: "install")

		var comps = URLComponents()
		comps.scheme = "https"
		comps.host = host
		comps.port = port
		comps.path = "/healthz"

		guard let url = comps.url else { return nil }

		do {
			let (_, response) = try await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 10))
			let status = (response as? HTTPURLResponse)?.statusCode ?? 0
			FileLogger.log("self check reached the server: HTTP \(status)", category: "install")
			return status == 200 ? nil : Abort(.badGateway, reason: "Installation server health check returned HTTP \(status).")
		} catch {
			FileLogger.error("self check could not reach \(url.absoluteString): \(error.localizedDescription)", category: "install")
			if LocalCertificate.isEnabled, !LocalCertificate.isTrusted {
				return NSError(domain: "Install", code: -1, userInfo: [
					NSLocalizedDescriptionKey: String.localized("The on-device certificate isn't trusted yet. Install its profile and turn on full trust in Settings › Installation.")
				])
			}
			return error
		}
	}
	
	/// Called on the main actor when a request finishes or the user stops it.
	func stop() {
		_acceptsUpdates = false
		payloadReads.close()
		backgroundTaskManager?.stop()
		backgroundTaskManager = nil
		_shutdownServer()
	}

	private func _shutdownServer() {
		let package = package
		self.package = nil
		guard let server = _server else { return }
		let needsShutdown = _needsShutdown
		_needsShutdown = false
		_server = nil
		FileLogger.log("installation server stopping id=\(id)", category: "install")
		DispatchQueue.global(qos: .utility).async {
			defer { withExtendedLifetime(package) {} }
			if needsShutdown { server.server.shutdown() }
			// Application resources also need cleanup when the listener failed to start.
			server.shutdown()
		}
	}
	
	private func _updateStatus(_ newStatus: InstallerStatusViewModel.InstallerStatus) {
		DispatchQueue.main.async {
			guard self._acceptsUpdates else { return }
			switch self.viewModel.status {
			case .completed, .broken: return
			case .sendingPayload, .installing:
				if case .sendingManifest = newStatus { return }
				if case .installing = self.viewModel.status, case .sendingPayload = newStatus { return }
			default: break
			}
			if case .sendingManifest = newStatus, self.backgroundTaskManager == nil {
				self.backgroundTaskManager = BackgroundTaskManager(
					taskName: "ServerInstaller", expirationTitle: "Installation continuing",
					expirationBody: "Keep the app open to complete the installation")
				self.backgroundTaskManager?.start()
			}
			self.viewModel.status = newStatus
		}
	}

	func getServerMethod() -> Int {
		UserDefaults.standard.integer(forKey: "Feather.serverMethod")
	}

	func getIPFix() -> Bool {
		UserDefaults.standard.bool(forKey: "Feather.ipFix")
	}
}
