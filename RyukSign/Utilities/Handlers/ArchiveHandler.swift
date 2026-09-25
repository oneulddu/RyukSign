//
//  ArchiveHandler.swift
//  RyukSign
//
//  Created by samara on 22.04.2025.
//

import Foundation
import UIKit.UIApplication
import Zip
import SwiftUI
import IDeviceSwift

final class ArchiveHandler: NSObject {
	let viewModel: InstallerStatusViewModel

	private let _fileManager = FileManager.default
	private let _uuid = UUID().uuidString
	private var _payloadUrl: URL?

	private let _appURL: URL?
	private let _appName: String?
	private let _appVersion: String?
	private let _uniqueWorkDir: URL
	
	// Snapshot Core Data-backed app values before packaging off the main actor, as in KorSign.
	@MainActor
	init(app: AppInfoPresentable, viewModel: InstallerStatusViewModel) {
		self.viewModel = viewModel
		self._appURL = Storage.shared.getAppDirectory(for: app)
		self._appName = app.name
		self._appVersion = app.version
		self._uniqueWorkDir = _fileManager.temporaryDirectory
			.appendingPathComponent("FeatherInstall_\(_uuid)", isDirectory: true)
		
		super.init()
	}
	
	func move() async throws {
		guard let appUrl = _appURL else {
			throw SigningFileHandlerError.appNotFound
		}
		
		let payloadUrl = _uniqueWorkDir.appendingPathComponent("Payload")
		let movedAppURL = payloadUrl.appendingPathComponent(appUrl.lastPathComponent)

		try _fileManager.createDirectoryIfNeeded(at: payloadUrl)
		
		try _fileManager.copyItem(at: appUrl, to: movedAppURL)
		_payloadUrl = payloadUrl
	}
	
	// Keep this async and nonisolated: with the app's Swift concurrency settings it runs
	// off the main actor at the caller's priority. The install/update pipeline owns keep-alive.
	func archive() async throws -> URL {
		guard let payloadUrl = self._payloadUrl else {
			throw SigningFileHandlerError.appNotFound
		}

		let ipaUrl = self._uniqueWorkDir.appendingPathComponent("Archive.ipa")
		let gate = ProgressGate()

		try AppArchiver.zip(
			payload: payloadUrl,
			to: ipaUrl,
			compression: ZipCompression.allCases[ArchiveHandler.getCompressionLevel()],
			progress: { progress in
				guard gate.admit(progress) else { return }
				Task { @MainActor in
					self.viewModel.packageProgress = progress
				}
			})

		return ipaUrl
	}
	
	func moveToArchive(_ package: URL, shouldOpen: Bool = false) async throws -> URL? {
		let appendingString = "\(_appName!)_\(_appVersion!)_\(Int(Date().timeIntervalSince1970)).ipa"
		let dest = _fileManager.archives.appendingPathComponent(appendingString)
		
		try? _fileManager.moveItem(
			at: package,
			to: dest
		)
		
		if shouldOpen {
			await MainActor.run {
				UIApplication.open(FileManager.default.archives.toSharedDocumentsURL()!)
			}
		}
		
		return dest
	}
	
	static func getCompressionLevel() -> Int {
		UserDefaults.standard.integer(forKey: "Feather.compressionLevel")
	}
}

// MARK: - Coalescing
/// Zip reports once per entry and an app bundle holds thousands of them; hopping to the main
/// actor for every one of them stalls SwiftUI for the whole archive.
final class ProgressGate {
	private let _lock = NSLock()
	private var _last = -1.0
	private let _step: Double

	init(step: Double = 0.01) {
		self._step = step
	}

	func admit(_ value: Double) -> Bool {
		_lock.lock()
		defer { _lock.unlock() }
		guard value >= 1 || value - _last >= _step else { return false }
		_last = value
		return true
	}
}
