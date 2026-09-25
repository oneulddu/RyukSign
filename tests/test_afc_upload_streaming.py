"""Compile the pairing-install AFC streamer and check bytes, chunking, errors, and memory."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
streamer = (root / 'IDeviceKitten/Sources/IDeviceSwift/InstallationProxy/ChunkedFileStreamer.swift').read_text()
proxy = (root / 'IDeviceKitten/Sources/IDeviceSwift/InstallationProxy/InstallationProxy.swift').read_text()
assert 'Data(contentsOf: url)' not in proxy, 'pairing install must not load the whole IPA'
assert 'ChunkedFileStreamer.stream(' in proxy

program = streamer + r'''
struct Stop: Error {}

func maxResidentBytes() -> Int {
    var usage = rusage()
    getrusage(RUSAGE_SELF, &usage)
    return Int(usage.ru_maxrss) // bytes on Darwin
}

func run(_ data: [UInt8], chunk: Int, dir: URL, failAt: Int? = nil) async throws -> ([Int], [UInt8], [Double]) {
    let url = dir.appendingPathComponent(UUID().uuidString)
    try Data(data).write(to: url)
    var sizes: [Int] = []
    var bytes: [UInt8] = []
    var progress: [Double] = []
    try await ChunkedFileStreamer.stream(fileAt: url, chunkSize: chunk, write: { part in
        if let failAt, sizes.count == failAt { throw Stop() }
        sizes.append(part.count)
        bytes.append(contentsOf: part)
    }, progress: { progress.append($0) })
    return (sizes, bytes, progress)
}

@main struct Check {
    static func main() async throws {
        let dir = URL(fileURLWithPath: CommandLine.arguments[1])
        let chunk = 1024
        for size in [0, 1, chunk - 1, chunk, chunk + 1, 3 * chunk + 5] {
            let data = (0..<size).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ 7) }
            let (sizes, bytes, progress) = try await run(data, chunk: chunk, dir: dir)
            precondition(bytes == data, "bytes differ for size \(size)")
            precondition(sizes.dropLast().allSatisfy { $0 == chunk }, "short middle chunk for \(size)")
            precondition(sizes.count == (size + chunk - 1) / chunk, "chunk count for \(size)")
            precondition(progress.count == sizes.count)
            precondition(zip(progress, progress.dropFirst()).allSatisfy { $0 <= $1 })
            if size > 0 { precondition(progress.last == 1) }
        }

        // A failed AFC write stops the upload and reaches the caller.
        do {
            _ = try await run([UInt8](repeating: 1, count: 5 * chunk), chunk: chunk, dir: dir, failAt: 2)
            preconditionFailure("write error was swallowed")
        } catch is Stop {}

        do {
            try await ChunkedFileStreamer.stream(fileAt: dir.appendingPathComponent("missing"),
                                                 write: { _ in preconditionFailure() },
                                                 progress: { _ in })
            preconditionFailure("missing file was accepted")
        } catch {}

        // Peak memory stays near one 64MB chunk for a file several times larger.
        let large = dir.appendingPathComponent("large.ipa")
        let largeSize = 768 * 1024 * 1024
        FileManager.default.createFile(atPath: large.path, contents: nil)
        let writer = try FileHandle(forWritingTo: large)
        let block = Data(repeating: 0xA5, count: 16 * 1024 * 1024)
        for _ in 0..<(largeSize / block.count) { writer.write(block) }
        try writer.close()

        let before = maxResidentBytes()
        var total = 0
        var checksum: UInt64 = 0
        try await ChunkedFileStreamer.stream(fileAt: large, write: { part in
            total += part.count
            checksum &+= UInt64(part[part.count - 1])
        }, progress: { _ in })
        let growth = maxResidentBytes() - before
        precondition(total == largeSize)
        precondition(growth < 160 * 1024 * 1024, "peak memory grew by \(growth) bytes")
        print("ok: streamed \(largeSize >> 20)MB, peak RSS growth \(growth >> 20)MB")
    }
}
'''

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    source = tmp / 'main.swift'
    source.write_text(program)
    binary = tmp / 'check'
    subprocess.run(['xcrun', 'swiftc', '-O', '-parse-as-library', str(source), '-o', str(binary)], check=True)
    data = tmp / 'data'
    data.mkdir()
    subprocess.run([str(binary), str(data)], check=True)
print('afc upload streaming: ok')
