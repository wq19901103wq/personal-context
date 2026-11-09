// Read-only EventKit bridge. Calendar writes deliberately do not exist here.
import EventKit
import Foundation

let output = URL(fileURLWithPath: CommandLine.arguments[1])
let args = Array(CommandLine.arguments.dropFirst(2))

func emit(_ value: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    try! data.write(to: output, options: .atomic)
}

func accessName() -> String {
    switch EKEventStore.authorizationStatus(for: .event) {
    case .fullAccess: return "full_access"
    case .writeOnly: return "write_only"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

let store = EKEventStore()
var authorizationError: String? = nil
if args.contains("authorize") && accessName() == "not_determined" {
    var finished = false
    store.requestFullAccessToEvents { _, error in
        DispatchQueue.main.async {
            if let error = error as NSError? { authorizationError = "\(error.domain):\(error.code)" }
            finished = true
        }
    }
    while !finished { RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1)) }
}
if args.contains("status") || args.contains("authorize") {
    var result: [String: Any] = ["access": accessName()]
    if let error = authorizationError { result["error"] = error }
    emit(result)
    exit(0)
}
guard accessName() == "full_access" else {
    emit(["error": "calendar_permission", "access": accessName()])
    exit(2)
}
guard args.count == 2, let start = Double(args[0]), let end = Double(args[1]),
      end > start, end - start < 366 * 86400 else {
    emit(["error": "invalid_range"])
    exit(2)
}
let calendars = store.calendars(for: .event).filter {
    $0.type == .local || $0.type == .calDAV || $0.type == .exchange
}
let metadata = calendars.map { calendar -> [String: Any] in
    ["id": calendar.calendarIdentifier, "name": calendar.title,
     "account": calendar.source.title]
}
// An empty calendar list means unavailable account data, never an empty event predicate.
var events: [[String: Any]] = []
if !calendars.isEmpty {
    let predicate = store.predicateForEvents(withStart: Date(timeIntervalSince1970: start),
        end: Date(timeIntervalSince1970: end), calendars: calendars)
    events = store.events(matching: predicate).map { event in
        let repeating = event.hasRecurrenceRules || event.isDetached
        let occurrence: Any = repeating ? (event.occurrenceDate ?? event.startDate).timeIntervalSince1970 : NSNull()
        let declined = event.attendees?.contains { $0.isCurrentUser && $0.participantStatus == .declined } ?? false
        let status = event.status == .canceled || declined ? "cancelled" :
            (event.status == .tentative ? "tentative" : "confirmed")
        let alarms = (event.alarms ?? []).compactMap { alarm -> Double? in
            let seconds = alarm.absoluteDate.map { event.startDate.timeIntervalSince($0) } ?? -alarm.relativeOffset
            return seconds >= 60 ? ceil(seconds / 60) : nil
        }
        return ["id": event.calendarItemExternalIdentifier ?? event.calendarItemIdentifier,
                "calendar_id": event.calendar.calendarIdentifier,
                "occurrence": occurrence, "title": event.title ?? "（无标题）",
                "notes": event.notes ?? "", "location": event.location ?? "",
                "starts_at": event.startDate.timeIntervalSince1970,
                "ends_at": event.endDate.timeIntervalSince1970,
                "all_day": event.isAllDay,
                "timezone": event.timeZone?.identifier ?? TimeZone.current.identifier,
                "status": status, "remind_before_minutes": alarms.max() as Any? ?? NSNull()]
    }
}
emit(["access": accessName(), "complete": true, "start": start, "end": end,
      "calendars": metadata, "events": events])
