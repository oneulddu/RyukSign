//
//  SourcesViewModel.swift
//  RyukSign
//
//  Created by samara on 30.04.2025.
//

import Foundation
import Combine
import AltSourceKit
import SwiftUI
import NimbleJSON
import OSLog

// MARK: - Class
final class SourcesViewModel: ObservableObject {
	static let shared = SourcesViewModel()

	typealias RepositoryDataHandler = Result<ASRepository, Error>

	private let _dataService = NBFetchService()

	/// `true` when no load is in progress (idle).
	@Published var isFinished = true
	@Published var sources: [AltSource: ASRepository] = [:]

	/// The single in-flight load. All loads are serialized through this.
	private var currentFetchTask: Task<Void, Never>?
	/// Source URLs of the last completed load; skips re-fetching an identical set.
	private var lastLoadedKey: Set<String>?
	private var backgroundTaskManager: BackgroundTaskManager?
	// Context of successfully loaded premium data; credentials never leave memory.
	private var premiumHeaders: [AltSource: [String: String]] = [:]
	private var premiumGeneration = 0
	private var premiumObserver: AnyCancellable?

	@MainActor
	private func repository(_ repository: ASRepository, attributedTo source: AltSource) -> ASRepository {
		var repository = repository
		for index in repository.apps.indices {
			repository.apps[index].sourceIdentifier = source.identifier
		}
		return repository
	}

	@MainActor
	private func hasCurrentAuthentication(_ source: AltSource) -> Bool {
		guard let url = source.sourceURL, RyukSignAPI.isPremiumSource(url) else { return premiumHeaders[source] == nil }
		return premiumHeaders[source] == RyukSignAPI.authHeaders(for: url)
	}

	@MainActor
	private func publish(_ working: inout [AltSource: ASRepository]) {
		// Authentication can change during any cache/network await, including another batch.
		working = working.filter { hasCurrentAuthentication($0.key) }
		premiumHeaders = premiumHeaders.filter { working[$0.key] != nil }
		sources = working
	}

	private func key(for sources: [AltSource]) -> Set<String> {
		Set(sources.compactMap { $0.sourceURL?.absoluteString })
	}

	@MainActor
	func refreshStale(_ sources: [AltSource]) async {
		if currentFetchTask == nil {
			lastLoadedKey = nil
		}
		await fetchSources(sources)
	}

	/// Loads every source's repository. Concurrent calls are serialized and coalesced.
	@MainActor
	func fetchSources(_ sources: FetchedResults<AltSource>, refresh: Bool = false, batchSize: Int = 4) async {
		await fetchSources(Array(sources), refresh: refresh, batchSize: batchSize)
	}

	@MainActor
	func fetchSources(_ sourcesArray: [AltSource], refresh: Bool = false, batchSize: Int = 4) async {
		if premiumObserver == nil {
			premiumObserver = PremiumManager.shared.$isActive.removeDuplicates().dropFirst().sink { [weak self] active in
				guard let self, !active else { return }
				// Reset can unregister the host without rotating the device credential.
				self.premiumGeneration += 1
				self.sources = self.sources.filter { self.premiumHeaders[$0.key] == nil }
				self.premiumHeaders.removeAll()
				self.lastLoadedKey = nil
			}
		}
		// Discard another account's data before waiting for an older request to finish.
		var visible = sources
		publish(&visible)
		let newKey = key(for: sourcesArray)

		// Coalesce: wait out any in-flight load. The task self-clears in its own
		// defer so awaiters exit instead of busy-spinning the main actor (0x8BADF00D).
		while let running = currentFetchTask {
			await running.value
		}

		// Same source set already loaded — skip (pull-to-refresh bypasses this).
		if !refresh, newKey == lastLoadedKey, sourcesArray.allSatisfy(hasCurrentAuthentication) {
			return
		}

		let task = Task {
			defer { self.currentFetchTask = nil }
			await self._performFetch(sourcesArray, key: newKey, force: refresh, batchSize: batchSize)
		}
		currentFetchTask = task
		await task.value
	}

	@MainActor
	private func _performFetch(_ sourcesArray: [AltSource], key: Set<String>, force: Bool, batchSize: Int) async {
		isFinished = false

		if backgroundTaskManager == nil {
			backgroundTaskManager = BackgroundTaskManager(
				taskName: "SourcesViewModel",
				expirationTitle: "Loading repositories",
				expirationBody: "Repository loading will continue when you reopen the app"
			)
			backgroundTaskManager?.start()
		}

		defer {
			lastLoadedKey = key
			isFinished = true
			backgroundTaskManager?.stop()
			backgroundTaskManager = nil
		}

		// Read CoreData (AltSource) fields on the main actor — it's not thread-safe.
		struct FetchItem {
			let source: AltSource
			let url: URL
			let headers: [String: String]
			let isPremium: Bool
			var cachedAt: Date?
		}

		var items: [FetchItem] = sourcesArray.compactMap { source in
			guard let url = source.sourceURL else {
				Logger.misc.error("Source has no URL: \(source.name ?? "Unknown", privacy: .public)")
				return nil
			}
			return FetchItem(
				source: source,
				url: RyukSignAPI.catalogURL(for: url),
				headers: RyukSignAPI.authHeaders(for: url),
				isPremium: RyukSignAPI.isPremiumSource(url)
			)
		}

		Logger.misc.info("fetchSources START: \(items.count, privacy: .public) sources")

		let service = _dataService
		let generation = premiumGeneration
		// Publish into a working copy cumulatively so `sources` is never blanked mid-load.
		var working: [AltSource: ASRepository] = [:]
		let cacheEnabled = SourceCache.isEnabled
		for item in items {
			working[item.source] = sources[item.source]
		}
		publish(&working)

		if cacheEnabled {
			// Small sources shouldn't wait on huge catalogs.
			await withTaskGroup(of: (index: Int, date: Date, repo: ASRepository)?.self) { group in
				for (index, item) in items.enumerated() where !item.isPremium {
					let url = item.url
					group.addTask(priority: .userInitiated) {
						guard let hit = SourceCache.data(for: url),
							let repo = try? JSONDecoder().decode(ASRepository.self, from: hit.data)
						else { return nil }
						return (index, hit.date, repo)
					}
				}
				for await hit in group {
					guard let hit else { continue }
					let source = items[hit.index].source
					items[hit.index].cachedAt = hit.date
					working = sources
					if working[source] == nil {
						working[source] = repository(hit.repo, attributedTo: source)
						publish(&working)
					}
				}
			}
		}

		if cacheEnabled {
			items = items.filter { item in
				item.isPremium || force || item.cachedAt.map(SourceCache.isStale) ?? true
			}
		}

		for startIndex in stride(from: 0, to: items.count, by: batchSize) {
			let endIndex = min(startIndex + batchSize, items.count)
			let batch = Array(items[startIndex..<endIndex])

			// Child tasks touch only Sendable values, never the NSManagedObject.
			let fetched: [(Int, ASRepository?)] = await withTaskGroup(of: (Int, ASRepository?).self) { group in
				for (offset, item) in batch.enumerated() {
					let globalIndex = startIndex + offset
					let url = item.url
					let headers = item.headers
					let isPremium = item.isPremium
					let cacheURL = cacheEnabled && !isPremium ? url : nil

					group.addTask {
						let repo: ASRepository? = await withCheckedContinuation { continuation in
							service.fetch(from: url, headers: headers, onData: { data in
								if let cacheURL { SourceCache.store(data, for: cacheURL) }
							}) { (result: RepositoryDataHandler) in
								switch result {
								case .success(let repo):
									continuation.resume(returning: repo)
								case .failure(let error):
									Logger.misc.error("Source fetch FAILED\(isPremium ? " [PREMIUM]" : "", privacy: .public) \(url.absoluteString, privacy: .public): \(error.localizedDescription, privacy: .public)")
									continuation.resume(returning: nil)
								}
							}
						}
						return (globalIndex, repo)
					}
				}

				var collected: [(Int, ASRepository?)] = []
				for await pair in group {
					collected.append(pair)
				}
				return collected
			}

			working = sources
			for (idx, repo) in fetched {
				let item = items[idx]
				if item.isPremium {
					guard generation == premiumGeneration,
						let url = item.source.sourceURL, RyukSignAPI.isPremiumSource(url),
						RyukSignAPI.authHeaders(for: url) == item.headers else {
						working.removeValue(forKey: item.source)
						premiumHeaders.removeValue(forKey: item.source)
						continue
					}
					if repo != nil { premiumHeaders[item.source] = item.headers }
				}
				if let repo {
					working[item.source] = repository(repo, attributedTo: item.source)
				}
			}

			publish(&working)
		}

		Logger.misc.info("fetchSources DONE: \(working.count, privacy: .public)/\(items.count, privacy: .public) loaded")
	}
}
