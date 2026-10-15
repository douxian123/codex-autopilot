// Codex 自动挡 · macOS 菜单栏 Companion
//
// 它是状态锚点，不是第二套面板：所有读写都走 ops/companion_api.py 这一条通道，
// 自己不另存任务账本，也不自己实现业务逻辑。
//
// 它负责四件用户能直接做的事：
//   1. 一眼看到自动挡在哪里、开没开、有没有事要你拍板
//   2. 随时派活（打字 / 截图 / 附件 / 拖到菜单栏图标上 / 系统服务菜单）
//   3. 一键开关总闸
//   4. 打开完整面板（浏览器里的驾驶舱）和状态文件

import AppKit
import Carbon.HIToolbox
import Combine
import Foundation
import SwiftUI

// MARK: - 状态模型（与 ops/companion_api.py status 输出一一对应）

struct CompanionStatus: Codable {
    var enabled: Bool
    var lifecycle: String
    var paused: Bool
    var switches: [String: Bool]
    var error: String?
    var project: String?
    var current_task: CurrentTask?
    var attention: [String]?
    var daemon: DaemonStatus?
    var recent_completed: [String]?
    var native: NativeStatus?
}

struct CurrentTask: Codable {
    var title: String?
    var stage: String?
    var detail: String?
}

struct DaemonStatus: Codable {
    var live: Bool?
    var job_running: Bool?
    var last_cycle: String?
    var project: String?
}

/// 原生直连只显示「已验证据」：socket 在不在是即时事实，
/// 能不能派活看自检结果（`last_probe` 为 nil = 从没跑过自检）。
struct NativeStatus: Codable {
    var socket_present: Bool?
    var history_db: Bool?
    var last_probe: LastProbe?
}

struct LastProbe: Codable {
    var connected: Bool?
    var can_dispatch: Bool?
    var transport: String?
    var reason: String?
    var at: String?
}

// MARK: - CompanionModel

final class CompanionModel: ObservableObject {
    @Published var status = CompanionStatus(
        enabled: false, lifecycle: "off", paused: false, switches: [:], error: nil
    )
    @Published var intake = ""
    @Published var attachments: [String] = []
    @Published var message = ""
    @Published var busy = false

    /// 需要把面板顶到用户眼前时回调（截图、附件、派活成功）。
    var onNeedsAttention: (() -> Void)?
    /// 打开浏览器里的完整面板。
    var onOpenPanel: (() -> Void)?

    private let scriptPath: String
    private let supportDir: URL

    init() {
        scriptPath = CompanionModel.resolveScriptPath()
        supportDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CodexAutopilot")
    }

    /// 控制接口只有两个来源：环境变量（调试）和安装器写下的 companion.json。
    /// 找不到就如实说找不到，不去猜别的路径。
    private static func resolveScriptPath() -> String {
        if let override = ProcessInfo.processInfo.environment["CODEX_AUTOPILOT_API"],
           !override.isEmpty {
            return override
        }
        let config = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CodexAutopilot/companion.json")
        if let data = try? Data(contentsOf: config),
           let decoded = try? JSONDecoder().decode([String: String].self, from: data),
           let api = decoded["api"], !api.isEmpty {
            return api
        }
        return ""
    }

    var controlScriptPath: String { scriptPath }

    // MARK: 调用控制接口

    private func call(_ args: [String], then: @escaping (Int32, [String: Any]?, String) -> Void) {
        let path = scriptPath
        guard !path.isEmpty else {
            message = "未配置控制接口：先跑一次 install.sh"
            return
        }
        busy = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            let out = Pipe()
            let err = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", path] + args
            process.standardOutput = out
            process.standardError = err
            var payload: [String: Any]?
            var text = ""
            var code: Int32 = -1
            do {
                try process.run()
                let outData = out.fileHandleForReading.readDataToEndOfFile()
                let errData = err.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                code = process.terminationStatus
                let merged = outData.isEmpty ? errData : outData + errData
                text = String(data: merged, encoding: .utf8) ?? ""
                payload = (try? JSONSerialization.jsonObject(with: outData)) as? [String: Any]
            } catch {
                text = error.localizedDescription
            }
            DispatchQueue.main.async {
                self?.busy = false
                then(code, payload, text)
            }
        }
    }

    func refresh() {
        call(["status"]) { [weak self] code, payload, text in
            guard let self else { return }
            if let payload,
               let data = try? JSONSerialization.data(withJSONObject: payload),
               let decoded = try? JSONDecoder().decode(CompanionStatus.self, from: data) {
                self.status = decoded
                // 只在真出错时覆盖提示，别把用户刚看到的自检结论刷掉。
                if let error = decoded.error { self.message = error }
            } else if code != 0 {
                self.message = text.isEmpty ? "控制接口没有返回状态" : text
            }
        }
    }

    /// 真跑一次原生直连自检（能不能把任务派进 Codex 窗口），结论原样贴出来。
    func nativeProbe() {
        call(["native-probe"]) { [weak self] _, payload, text in
            guard let self else { return }
            guard let native = payload?["native"] as? [String: Any] else {
                self.message = text.isEmpty ? "自检没有返回结果" : text
                return
            }
            if native["connected"] as? Bool == true {
                self.message = "原生直连已连通：可以把任务派进 Codex 窗口"
            } else if let reason = native["reason"] as? String, !reason.isEmpty {
                self.message = "原生直连不可用：" + reason
            } else {
                self.message = "原生直连不可用"
            }
            self.refresh()
        }
    }

    /// 原生直连这一行说什么，取决于有没有跑过自检——没跑过就说没跑过。
    var nativeLine: String {
        guard let native = status.native else { return "" }
        guard let probe = native.last_probe else {
            return "原生直连：未自检（点一次「原生自检」）"
        }
        if probe.connected == true {
            return "原生直连：已连通，可派活"
        }
        if native.socket_present == false {
            return "原生直连：Codex 没在运行"
        }
        return "原生直连：不可派活（只读历史）"
    }

    func toggle() {
        let action = status.enabled ? "disable" : "enable"
        call([action]) { [weak self] _, payload, text in
            guard let self else { return }
            self.message = (payload?["message"] as? String) ?? text
            self.refresh()
        }
    }

    func submit() {
        let text = intake.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !attachments.isEmpty else { return }
        var args = ["intake"]
        for path in attachments {
            args += ["--attach", path]
        }
        args.append(text.isEmpty ? "（见附件）" : text)
        call(args) { [weak self] code, payload, text in
            guard let self else { return }
            if code == 0 {
                let output = (payload?["output"] as? String) ?? ""
                self.message = output.isEmpty ? "已交给自动挡" : output
                self.intake = ""
                self.attachments = []
            } else {
                self.message = (payload?["message"] as? String)
                    ?? (payload?["output"] as? String)
                    ?? (text.isEmpty ? "派活失败" : text)
            }
            self.refresh()
        }
    }

    // MARK: 附件

    private func attachmentsDir() -> URL {
        let dir = supportDir.appendingPathComponent("attachments")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// 拖进菜单栏图标 / 选中文件时调用。附件复制进自动挡自己的目录，
    /// 引用的是副本：原文件被挪走或删掉，任务内容也不会失真。
    func attach(urls: [URL]) {
        let dir = attachmentsDir()
        var added = 0
        for url in urls {
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir),
                  !isDir.boolValue else { continue }
            if url.path.hasPrefix(dir.path) {
                if !attachments.contains(url.path) {
                    attachments.append(url.path)
                    added += 1
                }
                continue
            }
            let stamp = Int(Date().timeIntervalSince1970 * 1000)
            let dest = dir.appendingPathComponent("\(stamp)-\(url.lastPathComponent)")
            do {
                try FileManager.default.copyItem(at: url, to: dest)
            } catch {
                message = "附件复制失败：\(error.localizedDescription)"
                continue
            }
            if !attachments.contains(dest.path) {
                attachments.append(dest.path)
                added += 1
            }
        }
        if added > 0 {
            message = "已附上 \(attachments.count) 个附件，写一句话就能派活"
        }
    }

    func chooseFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "附上"
        panel.message = "选择要一起交给自动挡的文件（截图、日志、文稿都行）"
        if panel.runModal() == .OK {
            attach(urls: panel.urls)
        }
    }

    /// 交互式框选截图，存成附件。取消（Esc）就什么都不做。
    func capture() {
        guard !scriptPath.isEmpty else {
            message = "未配置控制接口：先跑一次 install.sh"
            return
        }
        let dir = attachmentsDir()
        let stamp = Int(Date().timeIntervalSince1970)
        let path = dir.appendingPathComponent("shot-\(stamp).png")
        message = "框选要发给自动挡的区域（Esc 取消）"
        onNeedsAttention?()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            process.arguments = ["-i", "-x", path.path]
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                DispatchQueue.main.async { self?.message = "截图失败：\(error.localizedDescription)" }
                return
            }
            let ok = FileManager.default.fileExists(atPath: path.path)
            DispatchQueue.main.async {
                guard let self else { return }
                if ok {
                    self.attachments.append(path.path)
                    self.message = "截图已附上，写一句话再派活"
                } else {
                    self.message = "已取消截图"
                }
                self.onNeedsAttention?()
            }
        }
    }

    // MARK: 面板与状态

    func openPanel() {
        if let handler = onOpenPanel {
            handler()
            return
        }
        call(["panel"]) { [weak self] code, payload, text in
            guard let self else { return }
            if code != 0 {
                self.message = (payload?["message"] as? String) ?? text
            }
        }
    }

    func openStatus() {
        call(["open", "status"]) { [weak self] _, payload, text in
            guard let self else { return }
            if let path = payload?["path"] as? String {
                NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "")
            } else {
                self.message = text.isEmpty ? "没有状态文件" : text
            }
        }
    }
}

// MARK: - 面板内容

struct CompanionView: View {
    @ObservedObject var model: CompanionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Circle()
                    .fill(model.status.enabled ? Color.green : Color.secondary)
                    .frame(width: 8, height: 8)
                Text("Codex 自动挡").font(.headline)
                Spacer()
                Button(model.status.enabled ? "关闭" : "开启") { model.toggle() }
                    .controlSize(.small)
            }
            Text(model.status.enabled ? "正在运行" : "已关闭，Codex 回到原生状态")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let project = model.status.project {
                Text(project)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            if model.status.daemon?.live != true, let project = model.status.daemon?.project {
                Text("后台守护当前服务：\(project)")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            }
            if !model.nativeLine.isEmpty {
                Text(model.nativeLine)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if let task = model.status.current_task {
                Divider()
                Text(task.stage ?? "当前任务")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(task.title ?? task.detail ?? "正在处理")
                    .font(.subheadline)
                    .lineLimit(2)
            }
            if let attention = model.status.attention, !attention.isEmpty {
                Divider()
                Text("需要你处理")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                ForEach(attention.prefix(3), id: \.self) { item in
                    Text("· " + item).font(.caption).lineLimit(2)
                }
            }
            Divider()
            if !model.attachments.isEmpty {
                Text("附件 \(model.attachments.count) 个")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            TextField("派一个新任务", text: $model.intake)
                .textFieldStyle(.roundedBorder)
                .onSubmit { model.submit() }
            HStack(spacing: 6) {
                Button("派活") { model.submit() }.controlSize(.small)
                Button("截图") { model.capture() }.controlSize(.small)
                Button("附件") { model.chooseFiles() }.controlSize(.small)
                Button("刷新") { model.refresh() }.controlSize(.small)
            }
            HStack(spacing: 6) {
                Button("完整面板") { model.openPanel() }.controlSize(.small)
                Button("原生自检") { model.nativeProbe() }.controlSize(.small)
                Button("状态文件") { model.openStatus() }.controlSize(.small)
                Spacer()
            }
            if !model.message.isEmpty {
                Text(model.message)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(12)
        .frame(width: 320)
    }
}

// MARK: - 菜单栏图标（同时是拖入目标）

final class StatusDropView: NSView {
    var onActivate: (() -> Void)?
    var onFiles: (([URL]) -> Void)?

    private var dragging = false
    private var hover = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        registerForDraggedTypes([.fileURL])
        addTrackingArea(NSTrackingArea(
            rect: .zero,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        ))
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) 不使用")
    }

    override func mouseDown(with event: NSEvent) {
        onActivate?()
    }

    override func mouseEntered(with event: NSEvent) {
        hover = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        hover = false
        needsDisplay = true
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard !draggedFiles(sender).isEmpty else { return [] }
        dragging = true
        needsDisplay = true
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        dragging = false
        needsDisplay = true
    }

    override func draggingEnded(_ sender: NSDraggingInfo) {
        dragging = false
        needsDisplay = true
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        dragging = false
        needsDisplay = true
        let files = draggedFiles(sender)
        guard !files.isEmpty else { return false }
        onFiles?(files)
        return true
    }

    private func draggedFiles(_ sender: NSDraggingInfo) -> [URL] {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let items = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self], options: options
        ) as? [URL]
        return items ?? []
    }

    override func draw(_ dirtyRect: NSRect) {
        guard dragging || hover else { return }
        let rect = bounds.insetBy(dx: 1.5, dy: 1.5)
        let path = NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4)
        let color = dragging ? NSColor.controlAccentColor : NSColor.tertiaryLabelColor
        color.withAlphaComponent(dragging ? 0.45 : 0.2).setFill()
        path.fill()
    }
}

// MARK: - 全局快捷键（Carbon，不需要辅助功能权限）

private var activeHotKeyCenter: HotKeyCenter?

final class HotKeyCenter {
    private var handlers: [UInt32: () -> Void] = [:]
    private var refs: [EventHotKeyRef?] = []
    private var nextID: UInt32 = 1

    init() {
        activeHotKeyCenter = self
        var spec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, _ -> OSStatus in
                guard let event, let center = activeHotKeyCenter else { return noErr }
                var hotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotKeyID
                )
                guard status == noErr else { return noErr }
                let id = hotKeyID.id
                DispatchQueue.main.async { center.handlers[id]?() }
                return noErr
            },
            1,
            &spec,
            nil,
            nil
        )
    }

    func register(keyCode: UInt32, modifiers: UInt32, handler: @escaping () -> Void) {
        let id = nextID
        nextID += 1
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x4155544F), id: id)
        let status = RegisterEventHotKey(
            keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &ref
        )
        guard status == noErr else { return }
        handlers[id] = handler
        refs.append(ref)
    }
}

// MARK: - 应用

final class CompanionAppDelegate: NSObject, NSApplicationDelegate {
    let model = CompanionModel()
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private var hotKeys: HotKeyCenter?
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        popover.behavior = .transient
        popover.contentSize = NSSize(width: 320, height: 340)
        popover.contentViewController = NSHostingController(rootView: CompanionView(model: model))

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = iconImage(enabled: false)
            button.toolTip = "Codex 自动挡"
            let overlay = StatusDropView(frame: button.bounds)
            overlay.autoresizingMask = [.width, .height]
            overlay.onActivate = { [weak self] in self?.togglePopover() }
            overlay.onFiles = { [weak self] urls in
                guard let self else { return }
                self.model.attach(urls: urls)
                self.showPopover()
            }
            button.addSubview(overlay)
        }

        model.onNeedsAttention = { [weak self] in self?.showPopover() }
        model.onOpenPanel = { [weak self] in self?.openDashboard() }
        model.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] status in
                self?.statusItem.button?.image = self?.iconImage(enabled: status.enabled)
            }
            .store(in: &cancellables)

        let center = HotKeyCenter()
        // ⌥⌘A 开合面板，⌥⌘S 截图派活，⌥⌘D 打开完整面板
        center.register(keyCode: UInt32(kVK_ANSI_A),
                        modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            self?.togglePopover()
        }
        center.register(keyCode: UInt32(kVK_ANSI_S),
                        modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            self?.model.capture()
        }
        center.register(keyCode: UInt32(kVK_ANSI_D),
                        modifiers: UInt32(cmdKey | optionKey)) { [weak self] in
            self?.openDashboard()
        }
        hotKeys = center

        NSApp.servicesProvider = self
        NSUpdateDynamicServices()

        model.refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.model.refresh()
        }
    }

    private func iconImage(enabled: Bool) -> NSImage? {
        let name = enabled ? "road.lanes" : "pause.circle"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "Codex 自动挡")
        image?.isTemplate = true
        return image
    }

    private func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
        } else {
            showPopover()
        }
    }

    private func showPopover() {
        guard let button = statusItem.button else { return }
        model.refresh()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        NSApp.activate(ignoringOtherApps: true)
    }

    /// 完整面板：让控制接口生成驾驶舱 HTML，再用系统默认程序打开。
    private func openDashboard() {
        let path = model.controlScriptPath
        guard !path.isEmpty else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", path, "panel"]
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            model.message = "打开面板失败：\(error.localizedDescription)"
        }
    }

    /// 系统服务菜单：选中文本或文件 → 服务 → 发送到 Codex 自动挡。
    @objc func sendToAutopilot(_ pboard: NSPasteboard, userData: String,
                               error: AutoreleasingUnsafeMutablePointer<NSString?>) {
        var accepted = false
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        if let urls = pboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL],
           !urls.isEmpty {
            model.attach(urls: urls)
            accepted = true
        }
        if let text = pboard.string(forType: .string),
           !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            model.intake = text
            accepted = true
        }
        if accepted {
            model.submit()
            showPopover()
            error.pointee = nil
        } else {
            error.pointee = "没有可发送的文本或文件" as NSString
        }
    }
}

@main
struct CodexAutopilotCompanion {
    static func main() {
        let app = NSApplication.shared
        let delegate = CompanionAppDelegate()
        app.delegate = delegate
        app.run()
    }
}
