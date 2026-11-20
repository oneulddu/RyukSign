"""Exercise production download lifecycle methods with a real loopback URLSession."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import tempfile
import threading
import time
import sys
from test_source_updates import declaration

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
source = (root / 'RyukSign/Backend/Observable/DownloadManager.swift').read_text()

def between(first, following):
    start = source.index(first)
    return source[start:source.index(following, start)]

methods = between('\tfunc startDownload(', '\n\tfunc startArchive(')
methods += between('\tfunc getDownloadTask(', '\n\tvar nonManualDownloads:')
methods += between('\tfunc pauseDownload(', '\n\tfunc pauseAllDownloads(')
methods += between('\tfunc getDownloadIndex(', '\n\tfunc getDownloadTask(')
methods += between('\tfunc handlePackageFile(', '\n}\n\n// MARK: - Source Apps')
delegates = (root / 'RyukSign/Backend/Observable/DownloadManager+Delegates.swift').read_text()
first = delegates.index('\tfunc urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo')
last = delegates.index('\tfunc urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError', first)
callbacks = delegates[first:last]
terminal = declaration(delegates, '\tfunc urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError')
callbacks += terminal.replace('func urlSession(', 'func terminalCompletion(', 1)
auto_sign = (root / 'RyukSign/Backend/Observable/AutoSignManager.swift').read_text().replace('import SwiftUI', '')
download_model = (root / 'RyukSign/Backend/Observable/Download.swift').read_text()
staging = download_model[download_model.index('\t/// Owned by this Download instance'):download_model.index('\tlet id: String')]
staging += declaration(download_model, '\tfunc stageFile(').replace('func stageFile(', 'func stageAfterLookup(')
staging += '\n' + declaration(download_model, '\tfunc removeStagedFiles(')
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
 var downloadStaging:URL {URL(fileURLWithPath:CommandLine.arguments[2]).appendingPathComponent("staging")}
 func createDirectory(at url:URL,withIntermediateDirectories make:Bool) throws {
  try Foundation.FileManager.default.createDirectory(at:url,withIntermediateDirectories:make)
 }
 func moveItem(at url:URL,to dst:URL) throws {try Foundation.FileManager.default.moveItem(at:url,to:dst)}
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
struct AppInfoPresentable { let version: String }
typealias Signed = AppInfoPresentable
struct CertificatePair {}
struct Options {
 enum SigningOption {case `default`, custom}
 let signingOption = SigningOption.custom
 let post_installAppAfterSigned = true
 static let batchBase = Options()
 func resolved(for app:AppInfoPresentable)->Self {self}
}
class OptionsManager {static let shared = OptionsManager(); let options = Options()}
class Storage {
 static let shared = Storage()
 func getCertificate(for index:Int)->CertificatePair? {nil}
 func deleteApp(for app:AppInfoPresentable) {}
}
class InstallQueue {
 static let shared = InstallQueue()
 var installs:[String] = []
 func enqueue(_ app:Signed) {installs.append(app.version)}
}
enum FR {
 static var signs:[String] = []
 static var finishHeldSign:(()->Void)?
 static func signPackageFile(_ app:AppInfoPresentable,using options:Options,icon:URL?,certificate:CertificatePair?,
                             completion:@escaping (Result<Signed,Error>)->Void) {
  signs.append(app.version)
  if app.version == "held-sign" {finishHeldSign = {completion(.success(app))}}
  else {completion(.success(app))}
 }
 static func handlePackageFile(_ url:URL, download:Download, completion:@escaping (Result<AppInfoPresentable,Error>)->Void) {
  do { download.importedBytes = try Data(contentsOf:url) } catch { completion(.failure(error)); return }
  let app = AppInfoPresentable(version:download.fileName)
  download.importSucceeded = { completion(.success(app)) }
 }
}
''' + auto_sign + r'''
enum Toast {
 enum Duration { case sticky }
 static func success(_ value:String,systemImage:String) {}
 static func error(_ value:String,duration:Duration) {}
}
extension String { static func localized(_ value:String)->String {value} }
class UINotificationFeedbackGenerator { enum Kind { case error }; func notificationOccurred(_ kind:Kind) {} }
class ErrorDelegate { func showUIErrorMessage(title:String,message:String) {} }
final class Download {
 let id: String; let url: URL; var isActive=false; var isPaused=false; var task:URLSessionDownloadTask?
 var action: AutoSignManager.Action?; var isImporting=false; var isSigning=false; var pendingFileURL:URL?
 var importedBytes:Data?; var importSucceeded:(()->Void)?
 var fileName:String { url.lastPathComponent }
 var resumeData:Data?
 var beforeStaging:(()->Void)?
''' + staging + r'''
 var bytesDownloaded:Int64=0; var totalBytes:Int64=0; var progress:Double=0; var unpackageProgress:Double=0
 func stageFile(at location:URL,suggestedFilename:String?) throws ->URL {
  beforeStaging?()
  return try stageAfterLookup(at:location,suggestedFilename:suggestedFilename)
 }
 init(id:String,url:URL,appName:String?,appDescription:String?) { self.id=id; self.url=url }
}
final class DownloadManager:NSObject,URLSessionDownloadDelegate,@unchecked Sendable {
 var downloads:[Download]=[]; var liveStarts=0; var isAppInBackground=false; let objectWillChange=Pulse()
 var _foregroundSession:URLSession!; var _backgroundSession:URLSession!
 let completed=DispatchSemaphore(value:0)
 let lock=NSLock(); var failure:Error?
 var completedDownloadNames:[String]=[]; var errorDelegate:ErrorDelegate?
 var downloadActivity:Any?; var finishedDownloadingIDs=Set<String>(); var allActivityDownloads:[String:Int]=[:]
 var lastUpdateTime=Date(); var updateThrottle:TimeInterval=2
 func saveResumeData(for download:Download) {}
 func releaseKeepAliveIfIdle() {}
 func endLiveActivity() {}
 func forceNextProgressUpdate() {}
 func updateLiveActivity(activeDownloads:[Download]) {}
 func sendCompletionNotification(for download:Download,status:String) {}
 func sendSystemNotification(title:String,body:String,identifier:String) {}
 func updateLiveActivityWithPausedState(activeDownloads:[Download]) {}
 func showUIErrorMessage(for download:Download,error:NSError) {}
 func beginImport(for download:Download) {download.isImporting=true}
 func endImport(for download:Download) {download.isImporting=false;download.pendingFileURL=nil}
 func dismissLiveActivityImmediately() {}
 override init() {
  super.init()
  let c=URLSessionConfiguration.ephemeral
  c.timeoutIntervalForRequest=5; c.timeoutIntervalForResource=10
  _foregroundSession=URLSession(configuration:c,delegate:self,delegateQueue:nil)
  _backgroundSession=_foregroundSession
 }
 func resumeDownload(_ d:Download) {} ; func startProgressTimer() {}; func ensureKeepAlive() {}; func startLiveActivityIfNeeded() { liveStarts+=1 }
''' + methods + '\n' + callbacks + r'''
 func urlSession(_ session:URLSession,task:URLSessionTask,didCompleteWithError error:Error?) {
  terminalCompletion(session,task:task,didCompleteWithError:error)
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
 let ok=done && manager.failure == nil && d.progress==1 && d.importedBytes==expected && d.bytesDownloaded==Int64(expected.count)
 print("\(ok ? "PASS" : "FAIL") \(path): progress=\(d.progress), bytes=\(d.bytesDownloaded)")
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
// Release import completions explicitly after the staged bytes have been copied.
// One cancelled job and two versions of one listing share the real completion path.
do {
 let manager=DownloadManager()
 func version(_ name:String, _ id:String)->Download {
  let url=URL(string:base+"/"+name)!
  let d=Download(id:id,url:url,appName:nil,appDescription:nil)
  d.isActive=true
  d.task=manager._foregroundSession.downloadTask(with:url)
  manager.downloads.append(d)
  return d
 }
 let cancelled=version("cancelled-import","cancel")
 let first=version("v1","listing")
 let second=version("v2","listing")
 cancelled.action = .install
 second.action = .install
 let staged=URL(fileURLWithPath:records+"/fast")
 try manager.handlePackageFile(url:staged,dl:cancelled)
 try manager.handlePackageFile(url:staged,dl:second)
 manager.cancelDownload(cancelled)
 cancelled.importSucceeded?()
 second.importSucceeded?()
 let deadline=Date().addingTimeInterval(5)
 while !manager.completedDownloadNames.contains("v2") && Date()<deadline {
  RunLoop.main.run(until:Date().addingTimeInterval(0.02))
 }
 let actionsOK=InstallQueue.shared.installs == ["v2"] && manager.completedDownloadNames == ["v2"]
 let ownersOK=manager.downloads.count == 1 && manager.getDownloadTask(by:first.task!) === first
 print("\(actionsOK ? "PASS":"FAIL") cancelled import did not dispatch install; installs=\(InstallQueue.shared.installs)")
 print("\(ownersOK ? "PASS":"FAIL") finishing the second version retained the first version's task owner")
 if !actionsOK || !ownersOK {failures+=1}

 func waitFor(_ predicate:()->Bool) -> Bool {
  let deadline=Date().addingTimeInterval(4)
  while !predicate() && Date()<deadline { RunLoop.main.run(until:Date().addingTimeInterval(0.01)) }
  return predicate()
 }
 let held=version("held-sign","held"), queued=version("queued-sign","queued"), live=version("live-sign","live")
 for d in [held,queued,live] {
  d.action = .install
  try manager.handlePackageFile(url:staged,dl:d)
  d.importSucceeded?()
 }
 let queuedBehindSigner=waitFor { FR.finishHeldSign != nil && queued.isSigning && live.isSigning }
 manager.cancelDownload(held)
 manager.cancelDownload(queued)
 FR.finishHeldSign?()
 let drained=waitFor {manager.completedDownloadNames.contains("live-sign")}
 let signOK=queuedBehindSigner && drained && FR.signs == ["v2","held-sign","live-sign"] && InstallQueue.shared.installs == ["v2","live-sign"]
 print("\(signOK ? "PASS":"FAIL") cancelled queued and in-flight signing: signed=\(FR.signs), installed=\(InstallQueue.shared.installs)")
 if !signOK {failures+=1}

 for code in [URLError.cannotFindHost, .badServerResponse] {
  let failed=version("failed","listing")
  manager.terminalCompletion(manager._foregroundSession,task:failed.task!,didCompleteWithError:URLError(code))
  let keptOwner=waitFor {manager.downloads.count == 1} && manager.getDownloadTask(by:first.task!) === first
  print("\(keptOwner ? "PASS":"FAIL") terminal \(code): sibling task remains registered")
  if !keptOwner {failures+=1}
 }

 let racing=version("finished-but-cancelled","racing")
 racing.beforeStaging={manager.cancelDownload(racing)}
 let temp=URL(fileURLWithPath:records+"/delegate-temp")
 try expected.write(to:temp)
 manager.urlSession(manager._foregroundSession,downloadTask:racing.task!,didFinishDownloadingTo:temp)
 let cleaned = !FileManager.default.fileExists(atPath:racing.stagingDirectory.path) && racing.importedBytes == nil
 print("\(cleaned ? "PASS":"FAIL") cancel after task lookup leaves no staged payload")
 if !cleaned {failures+=1}
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
