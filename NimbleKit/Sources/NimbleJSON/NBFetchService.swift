//
//  FetchService.swift
//  Loader
//
//  Created by samara on 14.03.2025.
//

import Foundation
import OSLog

// MARK: - Class
public class NBFetchService {

	private static let log = Logger(subsystem: "com.ryuksign.network", category: "NBFetchService")
	private static let uncachedSession: URLSession = {
		let configuration = URLSessionConfiguration.default
		configuration.urlCache = nil
		return URLSession(configuration: configuration)
	}()

	public enum NBFetchServiceError: Error, LocalizedError {
		case invalidURL
		case networkError(Error)
		case noData
		case httpError(Int)
		case parsingError(Error)

		public var errorDescription: String? {
			switch self {
			case .invalidURL: "The URL is invalid."
			case .networkError(let error): "Network error: \(error.localizedDescription)"
			case .noData: "No data received."
			case .httpError(let code): "Server returned HTTP \(code)."
			case .parsingError(let error): "Failed to parse data: \(error.localizedDescription)"
			}
		}
	}

	public init() {}

	public static func uncachedData(for request: URLRequest) async throws -> (Data, URLResponse) {
		var request = request
		request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
		return try await uncachedSession.data(for: request)
	}
}

// MARK: - Class extension: fetch
extension NBFetchService {
	public func fetch<T: Decodable>(
		from urlString: String,
		headers: [String: String] = [:],
		completion: @escaping (Result<T, Error>) -> Void
	) {
		guard let url = URL(string: urlString) else {
			completion(.failure(NBFetchServiceError.invalidURL))
			return
		}

		fetch(from: url, headers: headers, completion: completion)
	}

	public func fetch<T: Decodable>(
		from url: URL,
		headers: [String: String] = [:],
		onData: ((Data) -> Void)? = nil,
		completion: @escaping (Result<T, Error>) -> Void
	) {
		DispatchQueue.global(qos: .userInitiated).async {
			// Create URLRequest with gzip support
			var request = URLRequest(url: url)
			request.setValue("gzip, deflate, br", forHTTPHeaderField: "Accept-Encoding")
			request.cachePolicy = .reloadIgnoringLocalCacheData
			request.timeoutInterval = 30

			// Apply custom headers
			for (key, value) in headers {
				request.setValue(value, forHTTPHeaderField: key)
			}

			let session = request.value(forHTTPHeaderField: "Cache-Control") == "no-store" ? Self.uncachedSession : URLSession.shared
			let task = session.dataTask(with: request) { data, response, error in
				if let error = error {
					Self.log.error("Request FAILED \(url.absoluteString, privacy: .public): \(error.localizedDescription, privacy: .public)")
					completion(.failure(NBFetchServiceError.networkError(error)))
					return
				}

				let statusCode = (response as? HTTPURLResponse)?.statusCode ?? -1

				guard let data = data else {
					Self.log.error("Request NO DATA \(url.absoluteString, privacy: .public) (HTTP \(statusCode, privacy: .public))")
					completion(.failure(NBFetchServiceError.noData))
					return
				}

				if !(200..<300).contains(statusCode) {
					Self.log.error("Request HTTP \(statusCode, privacy: .public) \(url.absoluteString, privacy: .public) (\(data.count) bytes)")
					completion(.failure(NBFetchServiceError.httpError(statusCode)))
					return
				}

				// URLSession automatically decompresses gzip responses,
				// so we can use the data directly
				do {
					let decoder = JSONDecoder()
					let decodedData = try decoder.decode(T.self, from: data)
					Self.log.debug("Request OK (HTTP \(statusCode, privacy: .public)) \(url.absoluteString, privacy: .public)")
					onData?(data)
					completion(.success(decodedData))
				} catch {
					Self.log.error("Request PARSE FAIL (HTTP \(statusCode, privacy: .public)) \(url.absoluteString, privacy: .public) (\(data.count) bytes): \(String(describing: type(of: error)), privacy: .public)")
					completion(.failure(NBFetchServiceError.parsingError(error)))
				}
			}

			task.resume()
		}
	}
}
