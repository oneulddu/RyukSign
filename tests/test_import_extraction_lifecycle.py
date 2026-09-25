"""Compile production extraction with slow/failing extractor fixtures (macOS, swiftc).

Adapted from KorSign's extraction lifecycle test. No app build, real app data,
archive dependency, or network is needed; data and preferences stay temporary.
"""
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "RyukSign/Utilities/Handlers/AppFileHandler.swift").read_text()
ARCHIVE = (ROOT / "RyukSign/Utilities/Handlers/ArchiveHandler.swift").read_text()


def declaration(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


METHOD = declaration(SOURCE, "\tfunc extract() async throws {")
# As in the reference test, accelerate the historical five-minute deadline.
# Restoring that race must fail slow success without a five-minute test run.
METHOD = METHOD.replace(".seconds(300)", ".milliseconds(20)")
ERROR_HELPER = declaration(SOURCE, "\tprivate func _error(")
ERROR_TYPE = declaration(SOURCE, "struct ImportError:")
GATE = declaration(ARCHIVE, "final class ProgressGate")
# Instrument the real gate's entry only: its locking/throttling code is unchanged.
# This detects coalescing moved after the main-queue dispatch, even if UI counts match.
GATE = GATE.replace(
    "func admit(_ value: Double) -> Bool {",
    "func admit(_ value: Double) -> Bool {\n"
    "dispatchPrecondition(condition: .notOnQueue(.main))",
)

PROGRAM = r'''
import Foundation
import OSLog
import Darwin

@_silgen_name("check_extraction_queue") func checkExtractionQueue()
extension Logger {
 static let misc = Logger(subsystem: "ImportLifecycleTest", category: "test")
}
final class Download: @unchecked Sendable {
 var updates: [Double] = []
 var unpackageProgress = 0.0 {
  didSet {
   dispatchPrecondition(condition: .onQueue(.main))
   precondition(Thread.isMainThread)
   updates.append(unpackageProgress)
  }
 }
}
enum ArchiveExtraction {
 static func unzip(_ source: URL, to destination: URL, bufferSize: Int, useZlib: Bool,
                   progress: ((Double) -> Void)?) throws {
  checkExtractionQueue()
  precondition(!Thread.isMainThread)
  // The import path keeps KorSign's measured fast settings for every extension.
  precondition(bufferSize == 256 * 1024 && useZlib)
  let contents = try String(contentsOf: source, encoding: .utf8)
  precondition(contents == "archive fixture")
  // This exceeds the accelerated old timeout and leaves the main queue responsive.
  Thread.sleep(forTimeInterval: 0.12)
  if source.lastPathComponent.hasPrefix("failure") {
   throw NSError(domain: "Fixture.Zip", code: 73,
                 userInfo: [NSLocalizedDescriptionKey: "fixture extraction failure"])
  }
  if source.lastPathComponent.hasPrefix("context") {
   throw ImportError(.extract, fileName: "original.ipa", id: "original-id",
                     reason: "already contextualized")
  }
  for i in 0...10_000 { progress?(Double(i) / 10_000) }
  let payload = destination.appendingPathComponent("Payload")
  try FileManager.default.createDirectory(at: payload, withIntermediateDirectories: true)
  try Data("completed".utf8).write(to: payload.appendingPathComponent("finished"))
 }
}
final class Handler: @unchecked Sendable {
 let _ipa: URL
 let _uniqueWorkDir: URL
 static let extractionBufferSize = 256 * 1024
 let _uuid = "fixture-id"
 let _fileName: String
 let _download: Download?
 var uniqueWorkDirPayload: URL?
 init(_ name: String, root: URL, download: Download?) throws {
  _ipa = root.appendingPathComponent(name)
  _fileName = name
  _uniqueWorkDir = root.appendingPathComponent(UUID().uuidString)
  _download = download
  try Data("archive fixture".utf8).write(to: _ipa)
 }
''' + ERROR_HELPER + "\n" + METHOD + r'''
}
''' + ERROR_TYPE + "\n" + GATE + r'''

@main struct Check {
 @MainActor static func flushProgress() async {
  await withCheckedContinuation { continuation in
   DispatchQueue.main.async { continuation.resume() }
  }
 }
 @MainActor static func verifySuccess(_ handler: Handler) {
  let payload = handler.uniqueWorkDirPayload!
  precondition(payload == handler._uniqueWorkDir.appendingPathComponent("Payload"))
  precondition(FileManager.default.fileExists(atPath: payload.appendingPathComponent("finished").path))
  if let updates = handler._download?.updates {
   precondition(updates.first == 0 && updates.last == 1)
   precondition(updates.count >= 90 && updates.count <= 102,
                "Expected roughly 1% UI updates, got \(updates.count)")
   precondition(zip(updates, updates.dropFirst()).allSatisfy { $0 < $1 })
  }
 }
 @MainActor static func run() async throws {
  let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
  var mainQueueResponsive = false
  DispatchQueue.main.asyncAfter(deadline: .now() + 0.03) { mainQueueResponsive = true }
  // Separate downloads also verify each extraction gets a fresh progress gate.
  for name in ["success.ipa", "success.tipa", "success.zip"] {
   let handler = try Handler(name, root: root, download: Download())
   try await handler.extract()
   await flushProgress()
   verifySuccess(handler)
  }
  precondition(mainQueueResponsive, "Extraction blocked the main queue")
  let noDownload = try Handler("without-download.ipa", root: root, download: nil)
  try await noDownload.extract()
  verifySuccess(noDownload)

  let failure = try Handler("failure.ipa", root: root, download: Download())
  do {
   try await failure.extract()
   fatalError("Extraction failure swallowed")
  } catch let error as ImportError {
   precondition(error.step == .extract && error.fileName == "failure.ipa")
   precondition(error.id == "fixture-id")
   precondition(error.underlying?.domain == "Fixture.Zip" && error.underlying?.code == 73)
   precondition(error.reason.contains("fixture extraction failure"))
   precondition(error.localizedDescription.contains("Detail: Fixture.Zip (73)"))
   precondition(failure.uniqueWorkDirPayload == nil)
  }
  let context = try Handler("context.ipa", root: root, download: nil)
  do {
   try await context.extract()
   fatalError("Contextualized error swallowed")
  } catch let error as ImportError {
   precondition(error.fileName == "original.ipa" && error.id == "original-id")
   precondition(error.reason == "already contextualized" && error.underlying == nil)
   precondition(context.uniqueWorkDirPayload == nil)
  }

  let cancelled = try Handler("cancelled.ipa", root: root, download: Download())
  let task = Task { try await cancelled.extract() }
  // The blocking library cannot cancel: even an already-cancelled caller must
  // await completion before its cleanup can safely touch the extraction directory.
  task.cancel()
  try await task.value
  await flushProgress()
  verifySuccess(cancelled)
  print("PASS: slow success; failures and context; actual userInitiated queue; main UI; throttling before dispatch; ipa/tipa/zip; nil download; cancellation waits")
 }
 static func main() {
  Task { @MainActor in
   do { try await run(); exit(0) }
   catch { fatalError("Unexpected extraction result: \(error)") }
  }
  RunLoop.main.run()
 }
}
'''

# Inspect the *executing dispatch queue*, not Task priority or the requested QoS
# string. Thread QoS may be promoted independently by a waiting higher-priority task.
QUEUE_PROBE = r'''
#include <dispatch/dispatch.h>
#include <assert.h>
void check_extraction_queue(void) {
    dispatch_queue_t queue = dispatch_get_current_queue();
    assert(queue != dispatch_get_main_queue());
    assert(dispatch_queue_get_qos_class(queue, NULL) == QOS_CLASS_USER_INITIATED);
}
'''


def test_import_extraction_lifecycle():
    with tempfile.TemporaryDirectory(prefix="ryuksign-import-lifecycle-") as directory:
        work = Path(directory)
        for name in ("data", "preferences", "tmp", "module-cache"):
            (work / name).mkdir()
        env = dict(os.environ, CFFIXED_USER_HOME=str(work / "preferences"),
                   TMPDIR=str(work / "tmp"), CLANG_MODULE_CACHE_PATH=str(work / "module-cache"))
        (work / "check.swift").write_text(PROGRAM)
        (work / "queue.c").write_text(QUEUE_PROBE)
        subprocess.run(["clang", "-Wno-deprecated-declarations", "-c", str(work / "queue.c"),
                        "-o", str(work / "queue.o")], check=True, env=env, timeout=30)
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library",
                        "-module-cache-path", str(work / "module-cache"),
                        str(work / "check.swift"), str(work / "queue.o"),
                        "-o", str(work / "check")], check=True, env=env, timeout=90)
        subprocess.run([str(work / "check"), str(work / "data")],
                       check=True, env=env, timeout=15)


if __name__ == "__main__":
    test_import_extraction_lifecycle()
