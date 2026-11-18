import Foundation

enum AppLanguage: String, CaseIterable, Identifiable, Codable {
    case zh = "zh"
    case en = "en"

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .zh: return "中文"
        case .en: return "English"
        }
    }

    var shortLabel: String {
        switch self {
        case .zh: return "中文"
        case .en: return "EN"
        }
    }

    func tr(_ zh: String, _ en: String) -> String {
        self == .zh ? zh : en
    }
}
