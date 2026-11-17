"""Run production source-navigation, header invalidation and library reorder policy.

UIKit rendering is not simulated: only the state transitions are compiled with
value fixtures, so this does not claim device navigation or layout coverage.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def declaration(path, signature):
    text = (ROOT / path).read_text()
    start = text.index(signature)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


library = "RyukSign/Views/Library/LibraryView.swift"
sources = "RyukSign/Views/Sources/SourcesView.swift"
table = "RyukSign/Views/Sources/Apps/UIKit/SourceAppsTableRepresentableView.swift"

program = r'''
import Foundation
import Darwin

func require(_ condition: Bool, _ message: String) {
    guard condition else {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

final class LibraryPolicy {
    enum Scope { case all, signed, imported, updates }
    var _selectedScope = Scope.all
    var _searchText = ""
''' + declaration(library, "    private var _canReorderApps:") + r'''
    var canReorder: Bool { _canReorderApps }
}

struct Source: Hashable { let identifier: String? }
struct Repository { let appIDs: Set<String> }
enum AppNavigationManager {
    struct PendingAppNavigation {
        let sourceIdentifier: String?
        let appId: String
    }
}
final class NavigationPolicy {
    struct Model { var sources: [Source: Repository] = [:] }
    var viewModel = Model()
    var _sources: [Source] = []
    var _activeIndividualSource: Source?
    var _navigationSource: Source?
    var _shouldNavigateToAllRepos = false
    func appExistsInRepository(appId: String, repository: Repository) -> Bool {
        repository.appIDs.contains(appId)
    }
''' + declaration(sources, "\tprivate func handlePendingNavigation(") + r'''
    func navigate(_ request: AppNavigationManager.PendingAppNavigation?) {
        handlePendingNavigation(request)
    }
}

final class HeaderPolicy {
    enum Sort { case name, date }
    final class Coordinator {
        var sourcesVersion = 1
        var searchText = ""
        var sortOption = Sort.name
        var sortAscending = true
        var combinedNews = true
    }
    var sourcesVersion = 1
    var searchText = ""
    var sortOption = Sort.name
    var sortAscending = true
    var _combinedNews = true
''' + declaration(table, "    func detectChanges(") + r'''
}

@main struct Check {
    static func main() {
        let library = LibraryPolicy()
        for scope in [LibraryPolicy.Scope.all, .signed, .imported] {
            library._selectedScope = scope
            require(library.canReorder, "Complete library sections remain reorderable")
        }
        library._selectedScope = .updates
        require(!library.canReorder, "Updates filter must not reorder hidden apps")
        library._selectedScope = .all
        library._searchText = "one app"
        require(!library.canReorder, "Search results must not reorder hidden apps")

        let navigation = NavigationPolicy()
        let excluded = Source(identifier: "excluded-source")
        let other = Source(identifier: "other-source")
        navigation._sources = [other, excluded]
        navigation._activeIndividualSource = other
        navigation.viewModel.sources[other] = Repository(appIDs: ["same.app"])
        navigation.navigate(.init(sourceIdentifier: "excluded-source", appId: "same.app"))
        require(navigation._navigationSource == excluded,
                "Explicit source wins even when another source contains the same app")
        require(!navigation._shouldNavigateToAllRepos,
                "Explicit excluded source must not route into filtered All Repositories")
        navigation._navigationSource = nil
        navigation._activeIndividualSource = nil
        navigation.navigate(.init(sourceIdentifier: nil, appId: "same.app"))
        require(navigation._shouldNavigateToAllRepos,
                "Unscoped app navigation still opens All Repositories")

        let header = HeaderPolicy()
        let coordinator = HeaderPolicy.Coordinator()
        require(!header.detectChanges(coordinator: coordinator).hasChanges,
                "Unchanged header does not invalidate the table")
        header._combinedNews = false
        var change = header.detectChanges(coordinator: coordinator)
        require(change.newsChanged && change.hasChanges,
                "Disabling combined news invalidates an existing header")
        require(!change.needsReload && !change.sourcesChanged && !change.listChanged,
                "News preference must not rebuild the heavy app list")
        coordinator.combinedNews = false
        require(!header.detectChanges(coordinator: coordinator).hasChanges,
                "Updated header records the applied preference")
        header._combinedNews = true
        change = header.detectChanges(coordinator: coordinator)
        require(change.newsChanged, "Re-enabling combined news restores the header")
        print("PASS: filtered reorder, explicit-source navigation, live news invalidation")
    }
}
'''

with tempfile.TemporaryDirectory(prefix="ryuksign-source-ui-") as directory:
    directory = Path(directory)
    source = directory / "Check.swift"
    binary = directory / "check"
    source.write_text(program)
    subprocess.run(["swiftc", "-parse-as-library", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
