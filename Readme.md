<h1 align="center">Autodarts Raspberry Images</h1>

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
  <img src="docs/images/Webinterface_1.png" alt="Autodarts Raspberry Images Preview" width="100%">
  <img src="docs/images/Webinterface_2.png" alt="Autodarts Raspberry Images Preview" width="100%">
  <img src="docs/images/Webinterface_3.png" alt="Autodarts Raspberry Images Preview" width="100%">
</p>


[Beispielvideo 1](https://www.facebook.com/share/v/1DtWDdw2eK/)

[Beispielvideo 2](https://www.facebook.com/share/v/1DjUeZvd2H/)

<p align="center">
  <em>Click the preview image to watch the video.</em>
</p>

---

## 🇩🇪 Deutsch

### Beschreibung

Dieses Projekt stellt zwei vorbereitete Images für **Raspberry Pi 4** und **Raspberry Pi 5** bereit, die speziell für den Einsatz mit **Autodarts** optimiert wurden.

- **Raspberry Pi 4**: Headless-Installation inkl QR Codes

**[download_LINK](https://www.dropbox.com/scl/fo/z126u74p1wanknowicr4m/APmREuvI8KmQRlnzGJjGl8k?rlkey=w0jujfewnrlkze1inc3n13sqs&st=lnjxbwm4&dl=0)**
- **Raspberry Pi 5**: Full-Installation mit grafischer Oberfläche inkl QR Codes
Beim Systemstart wird Autodarts automatisch geladen, sodass das System direkt einsatzbereit ist.

**[download_LINK](https://www.dropbox.com/scl/fo/b8djqibxu9bt15tceodcr/AC09tAXZ4FONwj2Vnfx3I-I?rlkey=i08nazdjtdvcfllavzs72mrp3&st=azf4rzuj&dl=0)**

---

### Unterstützte Systeme

#### Raspberry Pi 4 – Headless Installation

Die Raspberry Pi 4 Variante ist als **Headless-System** ausgelegt.  
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

#### Raspberry Pi 5 – Full Installation

Die Raspberry Pi 5 Variante ist als **vollwertige Installation mit grafischer Oberfläche** vorgesehen.

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
**Bei mehreren Dartboars kann der Accespoint auf einen anderen Namen unbennatn werden**

Nach dem Verbinden mit diesem WLAN ist die Weboberfläche im Browser unter folgender Adresse erreichbar:

`http://10.77.0.1`

optional kann man zum Verbinden den QR_Code_1 Scannen und zum öffnen der Weboberfläche QR_Code_2
Der Qr Code ist so konzepiert das man diesen Drucken kan und auf den Kameraarm Kleben kann. Siehe Download Link

<p align="center">
  <img src="docs/images/QR Codes.png" alt="Autodarts Raspberry Images Preview" width="10%">
</p>

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

[Link zu den 3D Druckfiles](https://www.thingiverse.com/thing:7315470)
**WICHTIGE INFORMATION** Bitte installieren sie nicht das Script von Thinkreverse, diese ganzen Scripte sind in diesen Images schon vorhanden

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

**WLED-Unterstützung**

- **WLED ist vorinstalliert**
- bis zu **3 WLED-Controller** können gleichzeitig mit dem System betrieben werden
- direkter Zugriff auf WLED-Lichteffekte über die Weboberfläche

falls WLED verwendet wird, ist ein Geschlossenes Case empfehlenswert.
Für den Winmau Plasma Light Ring habe ich dafür eine kostengünstige Lösung

<p align="center">
  <img src="docs/images/Dart_Case.jpg" alt="Autodarts Raspberry Images Preview" width="50%">
</p>

[Link zu den 3D Druckfiles](https://www.thingiverse.com/thing:7315431)

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

## 🇬🇧 English

### Description

This project provides two prepared images for **Raspberry Pi 4** and **Raspberry Pi 5**, optimized for use with **Autodarts**.

- **Raspberry Pi 4**: Headless installation
- **Raspberry Pi 5**: Full installation with graphical interface

Autodarts starts automatically when the system boots, so the setup is ready to use immediately.

---

### Supported Systems

#### Raspberry Pi 4 – Headless Installation

The Raspberry Pi 4 version is designed as a **headless system**.  
That means **no monitor, mouse, or keyboard** is required.

Only the following need to be connected:

- the cameras
- optionally a LAN cable
- or a compatible Wi-Fi adapter

Because the Raspberry Pi 4 runs without a graphical desktop in this setup, control and display are handled through an external device such as:

- tablet
- notebook
- PC
- smartphone

#### Raspberry Pi 5 – Full Installation

The Raspberry Pi 5 version is intended as a **full installation with graphical interface**.

The following are required:

- monitor
- mouse
- keyboard
- Wi-Fi dongle or LAN cable

After boot, Autodarts opens automatically in the browser.

---

### Hardware Requirements

#### Raspberry Pi 4

- at least **2 GB RAM**
- **active cooling** required

**Note:**  
The Raspberry Pi 4 does not provide enough performance for a smooth graphical desktop in this use case. For that reason, this version is intentionally designed as a headless setup.

#### Raspberry Pi 5

- at least **4 GB RAM**
- **active cooling** required

---

### Network and Initial Setup

When the Raspberry Pi starts, it automatically creates an **access point**.

**Default credentials**

- **Wi-Fi name:** `Autodartsinstall1`
- **Password:** `Autodarts1234`

Once connected to this Wi-Fi, the web interface can be reached in the browser at:

`http://10.77.0.1`

---

### Connecting to Your Home Network

If the Raspberry Pi should connect to your home network or the internet via Wi-Fi, an **external Wi-Fi antenna or USB Wi-Fi adapter** is required.

The internal Wi-Fi chip of the Raspberry Pi is already used for the access point.  
If you connect the device via **LAN cable**, no additional Wi-Fi dongle is needed.

**Recommended Wi-Fi adapters**

1. **BrosTrend AC650 Linux USB WLAN Stick**
2. **AR9271 NetCard**  
   _(more affordable, but lower-quality alternative)_

The most important factors are not maximum speed, but:

- stable connection
- low latency
- low jitter
- no connection dropouts

---

### Optional Hardware: LED and Button

The system is prepared to optionally support an **LED** and a **button / switch**.

#### Safe Shutdown

If the button is pressed for more than **4 seconds**, the LED blinks quickly and the Raspberry Pi performs a **safe shutdown**.

This helps protect both the system and the SD card.

#### Restarting the Autodarts Boardmanager

If the button is pressed for about **3 seconds**, the **Autodarts Boardmanager** is restarted.

This can be especially useful during gameplay when:

- the system becomes slower
- darts are detected with a delay
- hits are not recognized reliably

In many cases, restarting the boardmanager is enough without rebooting the whole system.

---

### Web Interface Features

The web interface offers many functions for setup, diagnostics, and system management.

**General features**

- easy connection to a Wi-Fi network
- checks of Wi-Fi/LAN quality, packet loss, and speed
- direct access to the dartboard manager, even if it is not yet linked to an Autodarts account
- version switching
- full-screen focus and camera sharpness adjustment for each individual camera

**WLED support**

- **WLED is preinstalled**
- up to **3 WLED controllers** can be used at the same time
- direct access to WLED lighting effects through the web interface

**Admin area**

The admin area provides additional management functions:

- update the complete web panel
- install the **UVC Hack**
- enable or disable the firewall
- and more

---

### Third-Party Software

This project uses or references third-party software and components.  
All rights remain with the original authors.

- **Autodarts**  
  Repository: `https://github.com/ddhille/autodarts-releases`  
  License: **MIT License**

- **Darts Caller**  
  Repository: `https://github.com/lbormann/darts-caller`  
  License: **GNU GPL v3**

- **Darts WLED**  
  Repository: `https://github.com/lbormann/darts-wled`  
  License: **GNU GPL v3**

Please note that third-party software is subject exclusively to its respective license terms.

---

### Copyright and Usage Restrictions

**Copyright (c) 2026 Peter Rottmann**

Unless otherwise stated, the following usage restrictions apply only to the parts of this project created by me.

**This project is not Open Source.**  
**No use, modification, or redistribution without explicit written permission.**

**All rights reserved.**

It is not permitted to copy, modify, distribute, publish, sublicense,  
or use this software, in whole or in part, for commercial purposes  
without my prior written consent.

This software is provided without warranty.
