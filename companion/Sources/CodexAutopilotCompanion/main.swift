import AppKit
import Foundation
import SwiftUI

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

final class CompanionModel: ObservableObject {
    @Published var status = CompanionStatus(
        enabled: false,
        lifecycle: "off",
        paused: false,
        switches: [:],
        error: nil
    )
    @Published var intake = ""
    @Published var message = ""

    private let apiPath: String

    init() {
        apiPath = Self.resolveAPIPath()
        refresh()
    }

    private static func resolveAPIPath() -> String {
        if let override = ProcessInfo.processInfo.environment["CODEX_AUTOPILOT_API"],
           !override.isEmpty {
            return override
        }
        let support = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/CodexAutopilot/companion.json")
        if let data = try? Data(contentsOf: support),
           let config = try? JSONDecoder().decode([String: String].self, from: data),
           let path = config["api"], !path.isEmpty {
            return path
        }
        return ""
    }

    func refresh() {
        guard !apiPath.isEmpty else {
            message = "未配置自动挡控制接口"
            return
        }
        run(["status"]) { [weak self] data in
            guard let self else { return }
            do {
                let decoded = try JSONDecoder().decode(CompanionStatus.self, from: data)
                DispatchQueue.main.async {
                    self.status = decoded
                    self.message = decoded.error ?? ""
                }
            } catch {
                DispatchQueue.main.async { self.message = error.localizedDescription }
            }
        }
    }

    func toggle() {
        guard !apiPath.isEmpty else { return }
        run([status.enabled ? "disable" : "enable"]) { [weak self] data in
            if let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = result["message"] as? String {
                DispatchQueue.main.async { self?.message = message }
                return
            }
            DispatchQueue.main.async { self?.refresh() }
        }
    }

    func submit() {
        let text = intake.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !apiPath.isEmpty else { return }
        run(["intake", text]) { [weak self] data in
            let output = String(data: data, encoding: .utf8) ?? ""
            let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            DispatchQueue.main.async {
                if result?["ok"] as? Bool == true {
                    self?.message = result?["output"] as? String ?? "已加入自动挡任务"
                    self?.intake = ""
                } else {
                    self?.message = result?["message"] as? String ?? output
                }
                self?.refresh()
            }
        }
    }

    private func run(_ args: [String], completion: @escaping (Data) -> Void) {
        let path = apiPath
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            let stdout = Pipe()
            let stderr = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", path] + args
            process.standardOutput = stdout
            process.standardError = stderr
            do {
                try process.run()
                let output = stdout.fileHandleForReading.readDataToEndOfFile()
                let error = stderr.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let data = process.terminationStatus == 0 ? output : (
                    output.isEmpty ? error : output + error
                )
                completion(data)
            } catch {
                DispatchQueue.main.async { self?.message = error.localizedDescription }
            }
        }
    }
}

struct CompanionView: View {
    @ObservedObject var model: CompanionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle()
                    .fill(model.status.enabled ? Color.green : Color.secondary)
                    .frame(width: 8, height: 8)
                Text("Codex 自动挡")
                    .font(.headline)
                Spacer()
                Button(model.status.enabled ? "关闭" : "开启") {
                    model.toggle()
                }
            }
            Text(model.status.enabled ? "正在运行" : "已关闭，Codex 保持原生状态")
                .foregroundStyle(.secondary)
            if let project = model.status.project {
                Text(project).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            if model.status.daemon?.job_running == true,
               model.status.daemon?.live != true,
               let project = model.status.daemon?.project {
                Text("后台守护当前服务：\(project)")
                    .font(.caption).foregroundStyle(.orange).lineLimit(2)
            }
            if let task = model.status.current_task {
                Divider()
                Text(task.stage ?? "当前任务").font(.caption).foregroundStyle(.secondary)
                Text(task.title ?? task.detail ?? "正在处理")
                    .font(.subheadline).lineLimit(2)
            }
            if let attention = model.status.attention, !attention.isEmpty {
                Divider()
                Text("需要处理").font(.caption).foregroundStyle(.secondary)
                ForEach(attention.prefix(3), id: \.self) { item in
                    Text(item).font(.caption).lineLimit(2)
                }
            }
            Divider()
            TextField("派一个新任务", text: $model.intake)
                .textFieldStyle(.roundedBorder)
                .onSubmit { model.submit() }
            HStack {
                Button("派活") { model.submit() }
                Button("刷新") { model.refresh() }
                Spacer()
            }
            if !model.message.isEmpty {
                Text(model.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(14)
        .frame(width: 300)
    }
}

@main
struct CodexAutopilotCompanion: App {
    @StateObject private var model = CompanionModel()

    var body: some Scene {
        MenuBarExtra {
            CompanionView(model: model)
        } label: {
            Image(systemName: model.status.enabled ? "road.lanes" : "pause.circle")
        }
        .menuBarExtraStyle(.window)
    }
}
