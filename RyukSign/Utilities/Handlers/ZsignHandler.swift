//
//  ZsignHandler.swift
//  RyukSign
//
//  Created by samara on 17.04.2025.
//

import Foundation
import Zsign
import UIKit

final class ZsignHandler {
	private var _appUrl: URL
	private var _options: Options
	private var _certificate: CertificatePair?
	
	init(
		appUrl: URL,
		options: Options = OptionsManager.shared.options,
		cert: CertificatePair? = nil
	) {
		self._appUrl = appUrl
		self._options = options
		self._certificate = cert
	}
	
	func disinject() async throws {
		guard !_options.disInjectionFiles.isEmpty else {
			return
		}
		
		SigningLog.shared.info(.localized("Removing injected dylibs"))

		let bundle = Bundle(url: _appUrl)
		let execPath = _appUrl.appendingPathComponent(bundle?.exec ?? "").relativePath

		if !Zsign.removeDylibs(appExecutable: execPath, using: _options.disInjectionFiles) {
			throw SigningFileHandlerError.disinjectFailed
		}
	}
	
	func sign() async throws {
		guard let cert = _certificate else {
			throw SigningFileHandlerError.missingCertifcate
		}

		StdoutCapture.shared.start { SigningLog.shared.info($0) }
		defer { StdoutCapture.shared.stop() }

		let credentials = await MainActor.run {
			(Storage.shared.getFile(.provision, from: cert)?.path ?? "",
			 Storage.shared.getFile(.certificate, from: cert)?.path ?? "", cert.password ?? "")
		}
		_ = try SigningBundleFiles.executable(in: _appUrl)
		var callbackFailed = false
		let succeeded = Zsign.sign(
			appPath: _appUrl.relativePath,
			provisionPath: credentials.0,
			p12Path: credentials.1,
			p12Password: credentials.2,
			entitlementsPath: _options.appEntitlementsFile?.path ?? "",
			removeProvision: _options.removeProvisioning,
			completion: { success in
				if !success { callbackFailed = true }
			}
		)
		guard succeeded, !callbackFailed else { throw SigningFileHandlerError.signFailed }
		try verifyOutput()
	}
	
	func adhocSign() async throws {
		StdoutCapture.shared.start { SigningLog.shared.info($0) }
		defer { StdoutCapture.shared.stop() }

		_ = try SigningBundleFiles.executable(in: _appUrl)
		var callbackFailed = false
		let succeeded = Zsign.sign(
			appPath: _appUrl.relativePath,
			entitlementsPath: _options.appEntitlementsFile?.path ?? "",
			adhoc: true,
			removeProvision: _options.removeProvisioning,
			completion: { success in
				if !success { callbackFailed = true }
			}
		)
		guard succeeded, !callbackFailed else { throw SigningFileHandlerError.signFailed }
		try verifyOutput()
	}

	// SignNode can report success after skipping the main executable. Nested
	// Mach-O files and .app directories can be resources rather than signing targets.
	private func verifyOutput() throws {
		let executable = try SigningBundleFiles.executable(in: _appUrl)
		guard Zsign.checkSigned(appExecutable: executable.path) else {
			throw SigningValidationError("Missing or unreadable code signature: \(executable.lastPathComponent)")
		}
		let resources = _appUrl.appendingPathComponent("_CodeSignature/CodeResources")
		guard (try resources.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true else {
			throw SigningValidationError("Missing code signature resources: \(_appUrl.lastPathComponent)")
		}
	}
}

struct SigningValidationError: LocalizedError {
	let errorDescription: String?
	init(_ reason: String) { errorDescription = reason }
}

enum SigningBundleFiles {
	// Use the name zsign reads, not Bundle.executableURL's inferred fallback.
	static func executable(in bundle: URL) throws -> URL {
		let bundle = bundle.resolvingSymlinksInPath().standardizedFileURL
		if let name = NSDictionary(contentsOf: bundle.appendingPathComponent("Info.plist"))?["CFBundleExecutable"] as? String,
		   !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.utf8.contains(0) {
			let file = bundle.appendingPathComponent(name).resolvingSymlinksInPath().standardizedFileURL
			if file.path.hasPrefix(bundle.path + "/"),
			   (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true,
			   FileManager.default.isReadableFile(atPath: file.path) {
				return file
			}
		}
		throw SigningValidationError("Missing or unreadable executable in \(bundle.lastPathComponent)")
	}

}
