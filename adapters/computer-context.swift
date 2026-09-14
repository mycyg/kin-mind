// Read-only macOS adapter. No input, activation, screenshots or permission prompts.
import AppKit
import ApplicationServices

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

var text: [String] = []
var visited = 0
func inspect(_ element: AXUIElement, depth: Int) {
    if depth > 7 || visited >= 500 || text.joined().count > 18000 { return }
    visited += 1
    let role = attribute(element, kAXRoleAttribute) as? String ?? ""
    let subrole = attribute(element, kAXSubroleAttribute) as? String ?? ""
    if subrole.lowercased().contains("secure") || role == "AXSecureTextField" { return }
    for name in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, "AXURL"] {
        if let value = attribute(element, name) as? String, !value.isEmpty {
            let bounded = String(value.prefix(4000))
            if text.last != bounded { text.append(bounded) }
        }
    }
    if let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children { inspect(child, depth: depth + 1) }
    }
}

let workspace = NSWorkspace.shared
let front = workspace.frontmostApplication
let trusted = AXIsProcessTrusted()
if trusted, let app = front {
    let element = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(element, 0.3)
    if let windows = attribute(element, kAXWindowsAttribute) as? [AXUIElement] {
        for window in windows.prefix(3) { inspect(window, depth: 0) }
    }
}
let windows = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? [])
    .filter { ($0[kCGWindowLayer as String] as? Int ?? 1) == 0 }
    .prefix(30).map { ["application": $0[kCGWindowOwnerName as String] as? String ?? "",
                       "title": $0[kCGWindowName as String] as? String ?? ""] }
let value: [String: Any] = [
    "front_application": front?.localizedName ?? "", "bundle_id": front?.bundleIdentifier ?? "",
    "accessibility_available": trusted, "windows": Array(windows),
    "readable_text": text, "actor": "unknown", "capture": "on-demand-text-only"
]
let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
