import AppKit
import Foundation
import SwiftUI

struct CompanionStatus: Codable {
    var enabled: Bool
    var lifecycle: String
    var paused: Bool
    var switches: [String: Bool]
    var error: String?
}

final class CompanionModel: ObservableObject {
    @Published var status = CompanionStatus(
        enabled: true,
        lifecycle: "on",
        paused: false,
        switches: [:],
        error: nil
    )
    @Published var intake = ""
    @Published var message = ""

    private let apiPath: String

    init() {
        apiPath = ProcessInfo.processInfo.environment["CODEX_AUTOPILOT_API"]
            ?? "\(FileManager.default.currentDirectoryPath)/ops/companion_api.py"
        refresh()
    }

    func refresh() {
        run(["status"]) { [weak self] data in
            guard let self else { return }
            if let decoded = try? JSONDecoder().decode(CompanionStatus.self, from: data) {
                DispatchQueue.main.async { self.status = decoded }
            }
        }
    }

    func toggle() {
        run([status.enabled ? "disable" : "enable"]) { [weak self] _ in
            DispatchQueue.main.async { self?.refresh() }
        }
    }

    func submit() {
        let text = intake.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        run(["intake", text]) { [weak self] data in
            let output = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                self?.message = output
                self?.intake = ""
                self?.refresh()
            }
        }
    }

    private func run(_ args: [String], completion: @escaping (Data) -> Void) {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", apiPath] + args
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
            completion(pipe.fileHandleForReading.readDataToEndOfFile())
        } catch {
            DispatchQueue.main.async { self.message = error.localizedDescription }
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
