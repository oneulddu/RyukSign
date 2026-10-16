"""Exercise production download lifecycle methods with a real loopback URLSession."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tempfile
import threading
import time
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
source = (root / 'RyukSign/Backend/Observable/DownloadManager.swift').read_text()

def between(first, following):
    start = source.index(first)
    return source[start:source.index(following, start)]

methods = between('\tfunc startDownload(', '\n\tfunc startArchive(')
methods += between('\tfunc getDownloadTask(', '\n\tvar nonManualDownloads:')
methods += between('\tfunc pauseDownload(', '\n\tfunc pauseAllDownloads(')
delegates = (root / 'RyukSign/Backend/Observable/DownloadManager+Delegates.swift').read_text()
first = delegates.index('\tfunc urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo')
last = delegates.index('\tfunc urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError', first)
callbacks = delegates[first:last]
request_records = tempfile.TemporaryDirectory(prefix='ryuksign-download-requests-')
record_directory = Path(request_records.name)

payload = bytes(range(256)) * 256
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        (record_directory / self.path.strip('/')).touch()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            for i in range(0, len(payload), 1024):
                self.wfile.write(payload[i:i+1024]); self.wfile.flush()
                if self.path == '/slow': time.sleep(.01)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self, *args): pass

program = r'''
import Foundation
// Production cancelDownload removes saved resume data from Documents. Keep that I/O isolated.
final class FileManager {
 static let `default`=FileManager()
 func urls(for directory:Foundation.FileManager.SearchPathDirectory,in domain:Foundation.FileManager.SearchPathDomainMask)->[URL] {
  [URL(fileURLWithPath:CommandLine.arguments[2],isDirectory:true)]
 }
 func removeItem(at url:URL) throws {try Foundation.FileManager.default.removeItem(at:url)}
 func fileExists(atPath path:String)->Bool {Foundation.FileManager.default.fileExists(atPath:path)}
}
final class Pulse { func send() {} }
struct UIBackgroundTaskIdentifier: Equatable { static let invalid=UIBackgroundTaskIdentifier() }
final class UIApplication {
 static let shared=UIApplication(); var delegate:AnyObject?=AppDelegate()
 func beginBackgroundTask(_ handler:()->Void)->UIBackgroundTaskIdentifier { .invalid }
 func endBackgroundTask(_ id:UIBackgroundTaskIdentifier) {}
}
final class UNUserNotificationCenter {
 static let shared=UNUserNotificationCenter(); static func current()->UNUserNotificationCenter {shared}
 func removeDeliveredNotifications(withIdentifiers:[String]) {}
}
final class AppDelegate { static var submissions=0; func submitContinuedProcessingTask() {Self.submissions+=1} }
enum RyukSignAPI { static func applyAuthHeaders(to request: inout URLRequest) {} }
final class Download {
 let id: String; let url: URL; var isActive=false; var isPaused=false; var task:URLSessionDownloadTask?
 var resumeData:Data?
 func removeStagedFiles() {}
 var bytesDownloaded:Int64=0; var totalBytes:Int64=0; var progress:Double=0; var unpackageProgress:Double=0
 func stageFile(at location:URL,suggestedFilename:String?) throws ->URL {location}
 init(id:String,url:URL,appName:String?,appDescription:String?) { self.id=id; self.url=url }
}
final class DownloadManager:NSObject,URLSessionDownloadDelegate,@unchecked Sendable {
 var downloads:[Download]=[]; var liveStarts=0; var isAppInBackground=false; let objectWillChange=Pulse()
 var _foregroundSession:URLSession!; var _backgroundSession:URLSession!
 let completed=DispatchSemaphore(value:0)
 let lock=NSLock(); var finishedOwner:Download?; var fileBytes=Data(); var failure:Error?
 var downloadActivity:Any?; var finishedDownloadingIDs=Set<String>(); var allActivityDownloads:[String:Int]=[:]
 var lastUpdateTime=Date(); var updateThrottle:TimeInterval=2
 func saveResumeData(for download:Download) {}
 func releaseKeepAliveIfIdle() {}
 func endLiveActivity() {}
 func forceNextProgressUpdate() {}
 func updateLiveActivity(activeDownloads:[Download]) {}
 func sendCompletionNotification(for download:Download,status:String) {}
 func handlePackageFile(url:URL,dl:Download) throws {finishedOwner=dl; fileBytes=try Data(contentsOf:url)}
 override init() {
  super.init()
  let c=URLSessionConfiguration.ephemeral
  c.timeoutIntervalForRequest=5; c.timeoutIntervalForResource=10
  _foregroundSession=URLSession(configuration:c,delegate:self,delegateQueue:nil)
  _backgroundSession=_foregroundSession
 }
 func resumeDownload(_ d:Download) {} ; func startProgressTimer() {}; func ensureKeepAlive() {}; func startLiveActivityIfNeeded() { liveStarts+=1 }
''' + methods + callbacks + r'''
 func urlSession(_ session:URLSession,task:URLSessionTask,didCompleteWithError error:Error?) {
  lock.lock(); failure=error; lock.unlock(); completed.signal()
 }
}
let base=CommandLine.arguments[1]
let records=CommandLine.arguments[2]
let expected=Data((0..<65536).map {UInt8($0 % 256)})
var failures=0
for path in ["fast", "slow"] {
 let manager=DownloadManager()
 let d=manager.startDownload(from:URL(string:base+"/"+path)!)
 // Keep main occupied while a real delegate queue and network can run.
 Thread.sleep(forTimeInterval:0.3)
 let early=FileManager.default.fileExists(atPath:records+"/"+path)
 if early { print("FAIL \(path): server received a request before registration"); failures+=1 }
 let limit=Date().addingTimeInterval(8)
 var done=false
 while Date()<limit {
  RunLoop.main.run(until:Date().addingTimeInterval(0.02))
  if manager.completed.wait(timeout:.now()) == .success {done=true;break}
 }
 manager.lock.lock()
 let ok=done && manager.failure == nil && d.progress==1 && manager.finishedOwner === d && manager.fileBytes==expected && d.bytesDownloaded==Int64(expected.count)
 print("\(ok ? "PASS" : "FAIL") \(path): progress=\(d.progress), completion=\(manager.finishedOwner === d), bytes=\(d.bytesDownloaded)")
 manager.lock.unlock()
 if !ok {failures+=1}
 if !manager.downloads.contains(where:{$0 === d}) {failures+=1}
 manager._foregroundSession.invalidateAndCancel()
}

// A returned task may be canceled or paused before the queued registration runs.
for paused in [false,true] {
 let manager=DownloadManager()
 let d=manager.startDownload(from:URL(string:base+"/"+(paused ? "paused":"cancelled"))!)
 let oldSubmissions=AppDelegate.submissions
 if paused {manager.pauseDownload(d)} else {manager.cancelDownload(d)}
 RunLoop.main.run(until:Date().addingTimeInterval(0.2))
 let listed=manager.downloads.contains{$0 === d}
 let requested=FileManager.default.fileExists(atPath:records+"/"+(paused ? "paused":"cancelled"))
 let ok=listed == paused && !d.isActive && d.isPaused == paused && !requested && (paused || (manager.liveStarts==0 && AppDelegate.submissions==oldSubmissions))
 print("\(ok ? "PASS":"FAIL") pre-registration \(paused ? "pause":"cancel"): retained=\(listed), active=\(d.isActive)")
 if !ok { failures+=1 }
 manager._foregroundSession.invalidateAndCancel()
}
exit(failures==0 ? 0:1)
'''
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
try:
    with tempfile.TemporaryDirectory(prefix='ryuksign-download-start-') as directory:
        work = Path(directory); (work / 'Check.swift').write_text(program)
        subprocess.run(['swiftc', '-swift-version', '5', str(work / 'Check.swift'), '-o', str(work / 'check')], check=True)
        subprocess.run([str(work / 'check'), f'http://127.0.0.1:{server.server_port}', str(record_directory)], check=True, timeout=25)
finally:
    server.shutdown(); server.server_close(); request_records.cleanup()
