<p align="center">
  🇩🇪 Deutsch | <a href="README.en.md">🇬🇧 English</a>
</p>

<h1 align="center">Autodarts Raspberry Images</h1>

<p align="center">
  <a href="https://youtu.be/vZOtYZ-dGQs"> 
    <img src="https://img.youtube.com/vi/vZOtYZ-dGQs/maxresdefault.jpg" alt="Vorschauvideo" width="400">
  </a>
</p>

<h2 align="center">
  <a href="https://youtu.be/vZOtYZ-dGQs">Vorschauvideo ansehen</a>
</h2>

<p align="center">
  Prepared Raspberry Pi images for <strong>Autodarts</strong> on <strong>Raspberry Pi 4</strong> and <strong>Raspberry Pi 5</strong>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Raspberry%20Pi-4%20%26%205-C51A4A" alt="Raspberry Pi 4 & 5">
  <img src="https://img.shields.io/badge/Autodarts-Ready-success" alt="Autodarts Ready">
  <img src="https://img.shields.io/badge/Webpanel-Included-blue" alt="Webpanel Included">
  <img src="https://img.shields.io/badge/License-All%20Rights%20Reserved-black" alt="All Rights Reserved">
</p>

<p align="center">
  <img src="docs/images/Webinterface_1.png" alt="Autodarts Raspberry Images Preview" width="70%">
  <img src="docs/images/Webinterface_2.png" alt="Autodarts Raspberry Images Preview" width="70%">
  <img src="docs/images/Webinterface_3.png" alt="Autodarts Raspberry Images Preview" width="70%">
</p>

<p align="center">
  <em>Klicke auf das Vorschaubild, um das Video anzusehen.</em>
</p>

---

## 🇩🇪 Deutsch

### Beschreibung

Dieses Projekt stellt zwei vorbereitete Images für **Raspberry Pi 4** und **Raspberry Pi 5** bereit, die speziell für den Einsatz mit **Autodarts** optimiert wurden.

**ZUSATZNOTIZ: Bitte sobald wie möglich immer das Webpanel updaten, damit ihr die neuesten Features habt.**

- **Raspberry Pi 4**: Headless-Installation inklusive QR-Codes
  **[download_LINK](https://autodarts-pi4-download.peter-2b8.workers.dev/download)**
  
- **Raspberry Pi 5**: Full-Installation mit grafischer Oberfläche inklusive QR-Codes  
  Beim Systemstart wird Autodarts automatisch geladen, sodass das System direkt einsatzbereit ist.
  **[download_LINK](https://autodarts-pi5-download.peter-2b8.workers.dev/download)**
---

# 🎉 Download ist wieder verfügbar!

## ✅ Ab sofort kann wieder heruntergeladen werden.

**Das Update ist abgeschlossen. Vielen Dank für eure Geduld!**

---

**Installation** --> „Eigenes Image“ wählen

<p align="left">
  <img src="docs/images/Raspi_imager_1.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
  <img src="docs/images/Raspi_imager_2.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
</p>

[Video_Installation_1](https://youtu.be/MPp4fZqoqj4)  
[Video_Installation_2](https://youtu.be/VT4V8c9nuxs) 
[Video_Installation_3](https://youtu.be/ccjEAH0Mx2Q) 

---

### Unterstützte Systeme

#### Raspberry Pi 4 – Headless-Installation

Die Raspberry-Pi-4-Variante ist als **Headless-System** ausgelegt.  
Es werden **kein Monitor, keine Maus und keine Tastatur** benötigt.

Angeschlossen werden lediglich:

- die Kameras
- optional ein LAN-Kabel
- oder ein kompatibler WLAN-Stick

Da der Raspberry Pi 4 ohne grafische Oberfläche betrieben wird, erfolgt die Bedienung über ein externes Gerät, zum Beispiel:

- Tablet
- Notebook
- PC
- Smartphone

#### Raspberry Pi 5 – Full-Installation

Die Raspberry-Pi-5-Variante ist als **vollwertige Installation mit grafischer Oberfläche** vorgesehen.

Hierfür werden benötigt:

- Monitor
- Maus
- Tastatur
- WLAN-Dongle oder LAN-Kabel

Nach dem Start öffnet sich Autodarts automatisch im Browser.

---

### Hardware-Anforderungen

#### Raspberry Pi 4

- mindestens **2 GB RAM**
- **aktive Kühlung** erforderlich

**Hinweis:**  
Der Raspberry Pi 4 ist für diese Anwendung nicht leistungsstark genug, um dauerhaft eine flüssige grafische Oberfläche bereitzustellen. Deshalb wird hier bewusst auf eine Headless-Lösung gesetzt.

#### Raspberry Pi 5

- mindestens **4 GB RAM**
- **aktive Kühlung** erforderlich

---

### Netzwerk und Erstkonfiguration

Beim Start des Raspberry Pi wird automatisch ein **Access Point** erstellt.

**Standard-Zugangsdaten**

- **WLAN-Name:** `Autodartsinstall1`
- **Passwort:** `Autodarts1234`  
  **Bei mehreren Dartboards kann der Access Point auf einen anderen Namen umbenannt werden.**

Sollte jemand mal den Anmeldenamen für installationen etc. benötigen. Erst seit Image Datei ab 28.03.2026 möglich.
  - Name: Autodart
  - Pw: Autodarts1234

Nach dem Verbinden mit diesem WLAN ist die Weboberfläche im Browser unter folgender Adresse erreichbar:

`http://10.77.0.1`

**WICHTIG:** Mit dem Access Point nur zum Konfigurieren verbinden, nicht zum Spielen. Zum Spielen bitte wieder auf das Internet-/Heim-WLAN wechseln.

Optional kann man zum Verbinden den **QR_Code_1** scannen und zum Öffnen der Weboberfläche **QR_Code_2**.  
Der QR-Code ist so konzipiert, dass man ihn drucken und auf den Kameraarm kleben kann. Siehe Download-Link.

<p align="center">
  <img src="docs/images/QR Codes.png" alt="Autodarts Raspberry Images Preview" width="10%">
</p>

Das Bild ist so konzipiert, dass man es als Foto drucken kann (10x15 cm)  
(z. B. bei Bipa, dm oder anderen Märkten mit Fotodruck). Wenn man den Access Point auf einen anderen Namen ändert, gibt es auch andere QR-Codes zum Drucken.

Das Ändern des Access Points ist nur bei mehreren Dartboards (Raspis) im gleichen Raum notwendig.

[Link zu den QR-Codes](https://github.com/Jumbo125/Autodarts-Webinterface-Installation/releases/download/V1/QR_Codes.zip)

---

### Verbindung mit dem Heimnetzwerk

Wenn der Raspberry Pi per WLAN mit dem Heimnetzwerk oder dem Internet verbunden werden soll, ist eine **externe WLAN-Antenne bzw. ein USB-WLAN-Stick** erforderlich.

Der interne WLAN-Chip des Raspberry Pi wird bereits für den Access Point verwendet.  
Bei einer Verbindung per **LAN-Kabel** ist kein zusätzlicher WLAN-Stick notwendig.

**Empfohlene WLAN-Dongles**

1. **BrosTrend AC650 Linux USB WLAN-Stick**
2. **AR9271 NetCard**  
   _(günstigere, aber qualitativ schwächere Alternative)_

Wichtig ist dabei weniger die maximale Geschwindigkeit, sondern vor allem:

- stabile Verbindung
- geringe Latenz
- niedriger Jitter
- keine Verbindungsabbrüche

---

### Optionale Hardware: LED und Schalter

Das System ist darauf vorbereitet, optional eine **LED** sowie einen **Schalter / Taster** zu integrieren.

<p align="center">
  <img src="docs/images/Raspberry_1.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
  <img src="docs/images/Raspberry_2.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
  <img src="docs/images/GPIOS.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
</p>

[Link zu den 3D-Druckfiles](https://www.thingiverse.com/thing:7315470)

**WICHTIGE INFORMATION:** Bitte installiere nicht das Script von Thinkreverse. Diese ganzen Scripts sind in diesen Images bereits vorhanden.

#### Sicheres Herunterfahren

Wird der Schalter länger als **4 Sekunden** gedrückt, blinkt die LED schnell und der Raspberry Pi führt einen **Safe Shutdown** aus.

Dadurch werden System und SD-Karte geschützt.

#### Neustart des Autodarts Boardmanagers

Wird der Schalter für etwa **3 Sekunden** gedrückt, wird der **Autodarts Boardmanager** neu gestartet.

Das ist besonders hilfreich, wenn während des Spiels:

- das System träger reagiert
- Darts verzögert erkannt werden
- Treffer nicht zuverlässig erkannt werden

Ein Neustart des Boardmanagers kann in solchen Fällen oft bereits helfen, ohne das komplette System neu starten zu müssen.

---

### Funktionen der Weboberfläche

Die Weboberfläche bietet zahlreiche Funktionen zur Einrichtung, Diagnose und Bedienung des Systems.

**Allgemeine Funktionen**

- einfache Verbindung mit einem WLAN-Netzwerk
- Prüfung der WLAN-/LAN-Verbindung auf Stabilität, Paketverluste und Geschwindigkeit
- direkter Zugriff auf den Dartboardmanager, auch wenn dieser noch nicht mit einem Autodarts-Account verknüpft ist
- Wechsel zwischen verschiedenen Versionen
- Fokussieren und Scharfstellen jeder einzelnen Kamera im Vollbildmodus für bessere Genauigkeit
- u. v. m.

---

**WLED-Unterstützung**

- **WLED ist vorinstalliert**
- **zurzeit ist Darts-Hub nicht installiert, es wird nicht benötigt**
- bis zu **3 WLED-Controller** können gleichzeitig mit dem System betrieben werden
- direkter Zugriff auf WLED-Lichteffekte über die Weboberfläche

Falls WLED verwendet wird, ist ein geschlossenes Case empfehlenswert.  
Für den Winmau Plasma Light Ring habe ich dafür eine kostengünstige Lösung.

Der Link zum Download der WLED-Einstellungen folgt in Kürze.

<p align="center">
  <img src="docs/images/Dart_Case.jpg" alt="Autodarts Raspberry Images Preview" width="20%">
</p>

[Link zu den 3D-Druckfiles](https://www.thingiverse.com/thing:7315431)

**Darts-Hub**  
(Was ist Darts-Hub?)

- Vereinfacht gesagt ist Darts-Hub ein mächtiges, extrem gut durchdachtes Tool bzw. eine grafische Oberfläche, die unter anderem zum Bedienen verschiedener anderer Programme verwendet wird.
- Zum Steuern von LED-Effekten werden der `darts-caller`- und der `darts-wled`-Dienst benötigt. Das sind beides Systeme vom gleichen Entwickler.
- Damit man diese konfigurieren kann, benötigt man IT-Wissen.
- Damit es einfacher wird, gibt es den Darts-Hub, der diese beiden Systeme steuert.
- Das geht aber nicht am Raspberry Pi 4.
- Damit man dennoch LED-Effekte hat, habe ich eine eigene Oberfläche zum Einstellen dieser Effekte gemacht.
- Alle, die ein Raspberry-Pi-5-Setup haben, können diesen nachträglich installieren. Wenn ich Zeit habe, mache ich das zukünftig direkt für die Raspberry-Pi-5-Datei.

---

**Admin-Bereich**

Im Admin-Bereich stehen zusätzliche Verwaltungsfunktionen zur Verfügung:

- Update des kompletten Webpanels
- Installation des **UVC Hack**
- Aktivieren / Deaktivieren der Firewall
- weitere System- und Verwaltungsfunktionen

---

### Verwendete Drittsoftware

Dieses Projekt nutzt bzw. bezieht sich auf Software und Komponenten Dritter.  
Die jeweiligen Rechte verbleiben bei den ursprünglichen Urhebern.

- **Autodarts**  
  Repository: `https://github.com/ddhille/autodarts-releases`  
  Lizenz: **MIT License**

- **Darts Caller**  
  Repository: `https://github.com/lbormann/darts-caller`  
  Lizenz: **GNU GPL v3**

- **Darts WLED**  
  Repository: `https://github.com/lbormann/darts-wled`  
  Lizenz: **GNU GPL v3**

Bitte beachte, dass für Drittsoftware ausschließlich deren jeweilige Lizenzbedingungen gelten.

---

### Copyright und Nutzungsbeschränkung

**Copyright (c) 2026 Peter Rottmann**

Soweit nicht anders angegeben, beziehen sich die folgenden Nutzungsbeschränkungen ausschließlich auf die von mir erstellten Projektbestandteile.

**Dieses Projekt ist nicht Open Source.**  
**Keine Nutzung, Veränderung oder Weitergabe ohne ausdrückliche schriftliche Erlaubnis.**

**Alle Rechte vorbehalten.**

Es ist nicht gestattet, diese Software ganz oder teilweise zu kopieren,  
zu verändern, weiterzugeben, zu veröffentlichen, zu sublicenzieren  
oder kommerziell zu nutzen, außer mit meiner vorherigen schriftlichen Zustimmung.

Diese Software wird ohne Gewährleistung bereitgestellt.

---
