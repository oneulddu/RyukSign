//
//  CertificateFileHandler.swift
//  RyukSign
//
//  Created by samara on 15.04.2025.
//

import Foundation
import Darwin
import OSLog

final class CertificateFileHandler: NSObject {
	private let _fileManager = FileManager.default
	private let _uuid = UUID().uuidString
	
	private let _key: URL
	private let _provision: URL
	private let _keyPassword: String?
	private let _certNickname: String?
	private let _isDefault: Bool
	
	private var _copiedDirectory: URL?
	private var _didAddToDatabase = false
	private var _certPair: Certificate?
	
	init(
		key: URL,
		provision: URL,
		password: String? = nil,
		nickname: String? = nil,
		isDefault: Bool = false
	) {
		self._key = key
		self._provision = provision
		self._keyPassword = password
		self._certNickname = nickname
		self._isDefault = isDefault
		
		_certPair = CertificateReader(provision).decoded
		
		super.init()
	}
	
	func copy() async throws {
		guard
			(_certPair != nil)
		else {
			throw CertificateFileHandlerError.certNotValid
		}
		
		let destinationURL = try await _directory()

		try _fileManager.createDirectory(at: destinationURL.deletingLastPathComponent(), withIntermediateDirectories: true)
		// An existing directory is never ours to clean up.
		guard mkdir(destinationURL.path, mode_t(S_IRWXU)) == 0 else {
			throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
		}
		_copiedDirectory = destinationURL
		do {
			try _fileManager.copyItem(at: _key, to: destinationURL.appendingPathComponent(_key.lastPathComponent))
			try _fileManager.copyItem(at: _provision, to: destinationURL.appendingPathComponent(_provision.lastPathComponent))
		} catch {
			_cleanUnregisteredCopy()
			throw error
		}
	}
	
	func addToDatabase() async throws {
		do {
			try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
				Storage.shared.addCertificate(
					uuid: _uuid,
					password: _keyPassword,
					nickname: _certNickname,
					ppq: _certPair?.PPQCheck ?? false,
					expiration: _certPair?.ExpirationDate ?? Date(),
					isDefault: _isDefault
				) { error in
					if let error { continuation.resume(throwing: error) }
					else { continuation.resume() }
				}
			}
			_didAddToDatabase = true
			Logger.misc.info("[\(self._uuid)] Added to database")
		} catch {
			_cleanUnregisteredCopy()
			throw error
		}
	}

	private func _cleanUnregisteredCopy() {
		guard !_didAddToDatabase, let directory = _copiedDirectory else { return }
		try? _fileManager.removeItem(at: directory)
	}

	private func _directory() async throws -> URL {
		// Documents/Feather/Certificates/\(UUID)
		_fileManager.certificates(_uuid)
	}
}

private enum CertificateFileHandlerError: Error {
	case certNotValid
}
