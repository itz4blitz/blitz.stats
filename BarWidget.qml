import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// System stats segments: CPU (usage + temp), RAM, disk, and network link
// capacity, each with a mini progress bar where it makes sense. Values come
// from stats-collect.py; every section is a separate Process so a hung df
// can never stall the CPU sampler.
//
// Hovering opens a dropdown card (PopupCard triggerMode "hover") with one
// row per metric; clicking a row toggles that segment's presence in the bar.
BarWidget {
  id: root
  moduleName: "blitz.stats"

  property int cpuPct: 0
  property int cpuTempC: 0
  property int memPct: 0
  property real memUsedGB: 0
  property real memTotalGB: 0
  property int diskPct: 0
  property real diskUsedGB: 0
  property real diskTotalGB: 0
  property string netIface: ""
  property int netMbps: 0
  property int gpuPct: -1  // -1 = nvidia-smi unavailable
  property int gpuTempC: 0
  property int rxRate: 0  // bytes/sec on the resolved physical NIC
  property int txRate: 0

  readonly property bool showCpu: setting("showCpu", true)
  readonly property bool showRam: setting("showRam", true)
  readonly property bool showDisk: setting("showDisk", true)
  readonly property bool showNet: setting("showNet", true)
  readonly property bool showGpu: setting("showGpu", true)
  // Compact mode only removes secondary gauges on narrow displays.
  readonly property bool compactBar: root.bar ? root.bar.width < 1500 : false
  property var aiProviders: []
  readonly property string aiCollectorPath: {
    var url = String(Qt.resolvedUrl("ai_usage.py"))
    return url.startsWith("file://") ? url.substring(7) : url
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property color dimColor: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.55)
  readonly property color trackColor: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.25)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property real fontSize: Style.font.caption
  readonly property real rowFontSize: Style.font.bodySmall

  // One gauge color rule for every segment: urgent past 85%, themed fg below.
  function gaugeColor(pct) {
    return pct >= 85 ? urgentColor : foreground
  }

  // Qt.resolvedUrl hands back a QUrl object here, not a string, so coerce
  // before stripping the file:// scheme for Process.command.
  readonly property string collectorPath: {
    var url = String(Qt.resolvedUrl("stats-collect.py"))
    return url.startsWith("file://") ? url.substring(7) : url
  }

  function netSpeedText() {
    if (!netMbps) return "no link"
    if (netMbps >= 1000) {
      var g = netMbps / 1000
      return (g === Math.round(g) ? g : g.toFixed(1)) + " Gb/s"
    }
    return netMbps + " Mb/s"
  }

  function rateText(bytesPerSec) {
    if (bytesPerSec >= 1048576) return (bytesPerSec / 1048576).toFixed(1) + "M"
    if (bytesPerSec >= 1024) return Math.round(bytesPerSec / 1024) + "K"
    return Math.round(bytesPerSec) + "B"
  }

  function rateFillPct(bytesPerSec) {
    if (!netMbps) return 0
    return Math.min(100, Math.round(bytesPerSec * 8 / (netMbps * 1000000) * 100))
  }

  function sizeText(gb) {
    return gb >= 1000 ? Math.round(gb / 1000) + " TB" : Math.round(gb) + " GB"
  }

  function apply(payload) {
    try {
      var d = JSON.parse(String(payload))
    } catch (e) {
      return
    }
    if (d.cpu !== undefined) cpuPct = d.cpu
    if (d.memPct !== undefined) memPct = d.memPct
    if (d.memUsedGB !== undefined) memUsedGB = d.memUsedGB
    if (d.memTotalGB !== undefined) memTotalGB = d.memTotalGB
    if (d.tempC !== undefined && d.tempC !== null) cpuTempC = d.tempC
    if (d.diskPct !== undefined) diskPct = d.diskPct
    if (d.diskUsedGB !== undefined) diskUsedGB = d.diskUsedGB
    if (d.diskTotalGB !== undefined) diskTotalGB = d.diskTotalGB
    if (d.iface !== undefined) netIface = d.iface || ""
    if (d.mbps !== undefined) netMbps = d.mbps || 0
    if (d.rxBytesPerSec !== undefined) rxRate = d.rxBytesPerSec || 0
    if (d.txBytesPerSec !== undefined) txRate = d.txBytesPerSec || 0
    if (d.gpuPct !== undefined) gpuPct = d.gpuPct === null ? -1 : d.gpuPct
    if (d.gpuTempC !== undefined && d.gpuTempC !== null) gpuTempC = d.gpuTempC
  }

  function refresh() {
    if (!fastProc.running) fastProc.running = true
    if (!tempProc.running) tempProc.running = true
    if (!diskProc.running) diskProc.running = true
    if (!netProc.running) netProc.running = true
    if (!gpuProc.running) gpuProc.running = true
  }

  // Persist a per-widget setting the same way the clock persists a cycled
  // format: write the full entry into shell.json so it survives reloads.
  function setSetting(key, value) {
    var entry = { id: root.moduleName }
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  function toggleSegment(key) {
    setSetting(key, !setting(key, true))
  }

  function applyAi(payload) {
    try {
      var parsed = JSON.parse(String(payload))
      aiProviders = parsed.providers || []
    } catch (e) {
      aiProviders = []
    }
  }

  function aiProvider(id) {
    for (var i = 0; i < aiProviders.length; i++)
      if (aiProviders[i].id === id) return aiProviders[i]
    return null
  }

  function aiLimitText(provider, kind) {
    if (!provider || !provider.limits) return "—"
    for (var i = 0; i < provider.limits.length; i++) {
      if (provider.limits[i].kind === kind)
        return Math.round(Number(provider.limits[i].percent) * 100) + "%"
    }
    return "—"
  }

  function aiLimitValue(id, kind) {
    var provider = aiProvider(id)
    if (!provider || !provider.limits) return -1
    for (var i = 0; i < provider.limits.length; i++) {
      if (provider.limits[i].kind === kind) return Number(provider.limits[i].percent) * 100
    }
    return -1
  }

  function aiInlineText(id) {
    var provider = aiProvider(id)
    if (!provider || provider.status === "unavailable") return "—"
    var session = aiLimitText(provider, "session")
    var weekly = aiLimitText(provider, "weekly")
    if (session !== "—" && weekly !== "—") return session + "/" + weekly
    if (weekly !== "—") return "w" + weekly
    if (session !== "—") return "s" + session
    return provider.status === "ok" ? "—" : "!"
  }

  function aiCompactText(id) {
    var provider = aiProvider(id)
    if (!provider || provider.status !== "ok") return ""
    var weekly = aiLimitText(provider, "weekly")
    var session = aiLimitText(provider, "session")
    return weekly !== "—" ? weekly : session !== "—" ? session : ""
  }

  function aiIcon(id) {
    var provider = aiProvider(id)
    return provider && provider.icon ? provider.icon : "?"
  }

  visible: !vertical && (showCpu || showRam || showDisk || showNet || showGpu)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  IpcHandler {
    target: "blitz.stats"

    function refresh(): void {
      root.broadcast("refresh")
    }

    // Applied on this instance; the shell.json write below hot-reloads the
    // entry to every monitor's instance, so no broadcast is needed.
    function toggle(key: string): void {
      if (["showCpu", "showGpu", "showRam", "showDisk", "showNet"].indexOf(key) < 0) return
      root.toggleSegment(key)
    }
  }

  Process {
    id: fastProc
    command: [root.collectorPath, "fast"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apply(text)
    }
  }

  Process {
    id: tempProc
    command: [root.collectorPath, "temp"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apply(text)
    }
  }

  Process {
    id: diskProc
    command: [root.collectorPath, "disk"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apply(text)
    }
  }

  Process {
    id: netProc
    command: [root.collectorPath, "net"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apply(text)
    }
  }

  Process {
    id: gpuProc
    command: [root.collectorPath, "gpu"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.apply(text)
    }
  }

  Process {
    id: aiProc
    command: ["python3", root.aiCollectorPath]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyAi(text)
    }
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!fastProc.running) fastProc.running = true
  }

  Timer {
    interval: 5000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!tempProc.running) tempProc.running = true
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!aiProc.running) aiProc.running = true
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!diskProc.running) diskProc.running = true
  }

  Timer {
    interval: 10000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!netProc.running) netProc.running = true
  }

  Timer {
    interval: 5000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!gpuProc.running) gpuProc.running = true
  }

  component MiniBar : Rectangle {
    property real pct: 0
    width: Style.space(18)
    height: Style.space(3)
    radius: height / 2
    color: root.trackColor

    // Linear fill vs a 1 Gb/s pipe makes idle traffic sub-pixel; keep a 3px
    // sliver whenever any traffic exists so the bar reads as active.
    Rectangle {
      width: parent.pct > 0
        ? Math.max(3, Math.min(parent.width, Math.round(parent.width * parent.pct / 100)))
        : 0
      height: parent.height
      radius: parent.radius
      color: root.gaugeColor(parent.pct)
    }
  }

  component SegmentLabel : Text {
    text: ""
    color: root.dimColor
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    font.bold: false
  }

  component SegmentValue : Text {
    text: ""
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
  }

  // Hover dropdown -----------------------------------------------------------

  property bool cardOpen: false

  // Dwell before opening so a passing cursor doesn't flash the card; short
  // grace before closing so micro-gaps between widget and card don't snap it.
  Timer {
    id: openDwell
    interval: 400
    repeat: false
    running: button.tooltipHovered && !root.cardOpen
    onTriggered: if (button.tooltipHovered) root.cardOpen = true
  }

  Timer {
    id: closeGrace
    interval: 150
    repeat: false
    running: !button.tooltipHovered && !statsCard.containsMouse && root.cardOpen
    onTriggered: if (!button.tooltipHovered && !statsCard.containsMouse) root.cardOpen = false
  }

  component StatsRow : Item {
    id: row
    property string label: ""
    property string value: ""
    property real pct: -1
    property real pct2: -1  // >= 0 switches to dual ↓/↑ bars (pct = down)
    property bool segmentOn: true
    signal toggleRequested()

    width: rows.width
    implicitHeight: header.implicitHeight
      + (gauges.visible ? Style.space(5) + gauges.implicitHeight : 0)

    // Anchored, not Row-with-spacer: values update live and change width, so
    // an imperative spacer would go stale and shove the value into the edge.
    Item {
      id: header
      width: parent.width
      implicitHeight: Math.max(dot.height, Math.max(labelText.implicitHeight, valueText.implicitHeight))

      Rectangle {
        id: dot
        width: Style.space(4)
        height: width
        radius: width / 2
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        color: row.segmentOn ? root.foreground : "transparent"
        border.width: 1
        border.color: row.segmentOn ? root.foreground : root.dimColor
      }

      Text {
        id: labelText
        text: row.label
        anchors.left: dot.right
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        color: root.dimColor
        font.family: root.fontFamily
        font.pixelSize: root.rowFontSize
      }

      Text {
        id: valueText
        text: row.value
        anchors.left: labelText.right
        anchors.leftMargin: Style.space(8)
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideLeft
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: root.rowFontSize
      }
    }

    Column {
      id: gauges
      visible: row.pct >= 0
      width: parent.width
      anchors.top: header.bottom
      anchors.topMargin: Style.space(5)
      spacing: Style.space(3)

      // Dual mode: two labelled bars (network down/up), each vs capacity.
      Row {
        visible: row.pct2 >= 0
        width: parent.width
        spacing: Style.space(4)

        Text {
          id: downGlyph
          text: "↓"
          color: root.dimColor
          font.family: root.fontFamily
          font.pixelSize: root.rowFontSize
          anchors.verticalCenter: parent.verticalCenter
        }

        MiniBar {
          width: parent.width - downGlyph.width - parent.spacing
          height: Style.space(3)
          pct: row.pct
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Row {
        visible: row.pct2 >= 0
        width: parent.width
        spacing: Style.space(4)

        Text {
          id: upGlyph
          text: "↑"
          color: root.dimColor
          font.family: root.fontFamily
          font.pixelSize: root.rowFontSize
          anchors.verticalCenter: parent.verticalCenter
        }

        MiniBar {
          width: parent.width - upGlyph.width - parent.spacing
          height: Style.space(3)
          pct: row.pct2
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      MiniBar {
        visible: row.pct2 < 0
        width: parent.width
        height: Style.space(4)
        pct: row.pct
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: row.toggleRequested()
    }
  }

  PopupCard {
    id: statsCard
    anchorItem: button
    bar: root.bar
    owner: root
    triggerMode: "hover"
    open: root.cardOpen
    contentWidth: fittedContentWidth(Style.space(265))
    contentHeight: fittedContentHeight(rows.implicitHeight)

    Column {
      id: rows
      width: parent.width
      spacing: Style.space(10)

      StatsRow {
        label: "CPU"
        value: root.cpuPct + "% · " + (root.cpuTempC ? root.cpuTempC + "°C" : "—")
        pct: root.cpuPct
        segmentOn: root.showCpu
        onToggleRequested: root.toggleSegment("showCpu")
      }

      StatsRow {
        label: "GPU"
        value: root.gpuPct >= 0
          ? root.gpuPct + "% · " + (root.gpuTempC ? root.gpuTempC + "°C" : "—")
          : "unavailable"
        pct: root.gpuPct >= 0 ? root.gpuPct : -1
        segmentOn: root.showGpu
        onToggleRequested: root.toggleSegment("showGpu")
      }

      StatsRow {
        label: "RAM"
        value: root.memPct + "% · " + root.sizeText(root.memUsedGB) + " / " + root.sizeText(root.memTotalGB)
        pct: root.memPct
        segmentOn: root.showRam
        onToggleRequested: root.toggleSegment("showRam")
      }

      StatsRow {
        label: "SSD"
        value: root.diskPct + "% · " + root.sizeText(root.diskUsedGB) + " / " + root.sizeText(root.diskTotalGB)
        pct: root.diskPct
        segmentOn: root.showDisk
        onToggleRequested: root.toggleSegment("showDisk")
      }

      StatsRow {
        label: "NET"
        value: (root.netIface || "—") + " · " + root.netSpeedText()
          + " · ↓" + root.rateText(root.rxRate) + "/s ↑" + root.rateText(root.txRate) + "/s"
        pct: root.rateFillPct(root.rxRate)
        pct2: root.rateFillPct(root.txRate)
        segmentOn: root.showNet
        onToggleRequested: root.toggleSegment("showNet")
      }

    }
  }

  // Bar segments -------------------------------------------------------------

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    horizontalMargin: 8.5
    fixedWidth: content.implicitWidth + 17
    tooltipText: ""

    Row {
      id: content
      anchors.centerIn: parent
      spacing: Style.space(8)

      Row {
        visible: root.showCpu
        spacing: Style.space(3)
        SegmentLabel { text: "cpu"; anchors.verticalCenter: parent.verticalCenter }
        MiniBar { visible: !root.compactBar; pct: root.cpuPct; anchors.verticalCenter: parent.verticalCenter }
        SegmentValue {
          text: root.cpuPct + "%" + (root.cpuTempC ? " " + root.cpuTempC + "°" : "")
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Row {
        visible: root.showGpu && (!root.compactBar || root.showCpu)
        spacing: Style.space(3)
        SegmentLabel { text: "gpu"; anchors.verticalCenter: parent.verticalCenter }
        MiniBar { visible: !root.compactBar; pct: Math.max(0, root.gpuPct); anchors.verticalCenter: parent.verticalCenter }
        SegmentValue {
          text: root.gpuPct >= 0
            ? root.gpuPct + "%" + (root.gpuTempC ? " " + root.gpuTempC + "°" : "")
            : "—"
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Row {
        visible: root.showRam
        spacing: Style.space(3)
        SegmentLabel { text: "ram"; anchors.verticalCenter: parent.verticalCenter }
        MiniBar { visible: !root.compactBar; pct: root.memPct; anchors.verticalCenter: parent.verticalCenter }
        SegmentValue { text: root.memPct + "%"; anchors.verticalCenter: parent.verticalCenter }
      }

      Row {
        visible: root.showDisk
        spacing: Style.space(3)
        SegmentLabel { text: "ssd"; anchors.verticalCenter: parent.verticalCenter }
        MiniBar { visible: !root.compactBar; pct: root.diskPct; anchors.verticalCenter: parent.verticalCenter }
        SegmentValue { text: root.diskPct + "%"; anchors.verticalCenter: parent.verticalCenter }
      }

      Row {
        visible: root.showNet && !root.compactBar
        spacing: Style.space(3)
        SegmentLabel { text: "net"; anchors.verticalCenter: parent.verticalCenter }
        MiniBar {
          pct: root.rateFillPct(Math.max(root.rxRate, root.txRate))
          anchors.verticalCenter: parent.verticalCenter
        }
        SegmentValue {
          text: "↓" + root.rateText(root.rxRate) + " ↑" + root.rateText(root.txRate)
          anchors.verticalCenter: parent.verticalCenter
        }
      }

    }
  }

}
