# Filament Stock

[![CI](https://github.com/anonymouse-labs/ha-filament-stock/actions/workflows/ci.yml/badge.svg)](https://github.com/anonymouse-labs/ha-filament-stock/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Home Assistant integration that keeps track of your 3D printing filament.

- Reads your spools from the **Bambu Lab cloud**: the filament list you manage in Bambu Studio or the Bambu Handy app.
- Reads your **print queue** from a Google Drive folder with sliced plate files and works out how much filament each color needs.
- Shows the **balance per color**: stock minus needed.
- **Deducts filament automatically** after every print tracked by [ha-bambulab](https://github.com/greghesp/ha-bambulab).
- Sends **notifications**, preferably through [WhatsApp Notifier](https://github.com/anonymouse-labs/ha-whatsapp-notifier).

The Bambu Lab cloud is the source of your stock. Without a Bambu Lab account the integration does not work.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Google Drive setup](#google-drive-setup)
- [Configuration](#configuration)
- [Entities](#entities)
- [Actions](#actions)
- [Color matching](#color-matching)
- [Deduction rules](#deduction-rules)
- [Notifications](#notifications)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Privacy and security](#privacy-and-security)
- [Credits](#credits)

## How it works

1. Every 15 minutes the integration reads your spools from the Bambu Lab cloud, read-only. Spools are grouped into **color groups** by material, color and note. Two spools with the same color and material but a different note (for example `matte` and `glossy`) are two color groups with their own stock.
2. Every 10 minutes it reads the sliced files (`*.gcode.3mf`) in your Google Drive folder and adds up the grams per color. That is what you **need**.
3. Each file color is matched to a color group (see [Color matching](#color-matching)). **Balance** is stock minus needed.
4. When ha-bambulab reports that a print started, the file and the grams per color are stored. When the print finishes or fails, the used filament is deducted (see [Deduction rules](#deduction-rules)).

By default nothing is written to the Bambu Lab cloud. Usage is kept in a local ledger per spool. When you change the remaining weight of a spool in Bambu Studio, the ledger of that spool starts again from the new cloud weight, so nothing is ever deducted twice.

## Requirements

- Home Assistant 2026.10 or newer.
- A Bambu Lab account with your spools in the filament list of Bambu Studio or Bambu Handy.
- A Google account for the Drive folder with your sliced plates.
- Optional, for automatic deduction: [ha-bambulab](https://github.com/greghesp/ha-bambulab) with your printer.
- Optional, for notifications: [WhatsApp Notifier](https://github.com/anonymouse-labs/ha-whatsapp-notifier) or any `notify` entity.

## Installation

### Marketplace (HACS)

1. In Home Assistant open **Settings > Marketplace**, use **Add from link** and paste `https://github.com/anonymouse-labs/ha-filament-stock`.
2. Install **Filament Stock** and restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and choose **Filament Stock**.

### Manual

1. Download `filament_stock.zip` from the [latest release](https://github.com/anonymouse-labs/ha-filament-stock/releases).
2. Unpack it to `config/custom_components/filament_stock/`.
3. Restart Home Assistant and add the integration.

## Google Drive setup

The integration reads the folder with a Google **service account**, read-only (`drive.readonly`). You create it yourself, it is free.

1. Open the [Google Cloud Console](https://console.cloud.google.com/projectcreate) and create a project, or pick an existing one.
2. [Enable the Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) for that project.
3. Open [Service accounts](https://console.cloud.google.com/iam-admin/serviceaccounts) and click **Create service account**. A name is enough, skip the roles.
4. Open the new service account, tab **Keys**, then **Add key > Create new key > JSON**. Your browser downloads a `.json` file. **Never paste this file in a chat, issue or forum.**
5. Copy the service account email from that file (`client_email`, ends with `iam.gserviceaccount.com`).
6. In [Google Drive](https://drive.google.com/drive/my-drive) on the web, right-click the folder with your sliced plates, choose **Share** and add that email as **Viewer**.
7. Open the folder. The address bar shows `drive.google.com/drive/folders/<ID>`.
8. When you add the integration, paste the full contents of the JSON file and the folder address (or only the ID).

Only files directly in that folder are read, not subfolders. Move printed plates to another folder (for example `done`) and they stop counting as needed. Their deduction is not lost.

### How to make a sliced file

In Bambu Studio slice the plate, then choose **File > Export > Export plate sliced file**. This gives a `.gcode.3mf`. A normal project file (`.3mf` without slicing) has no filament usage. It is listed as **Not sliced** and does not count.

## Configuration

Adding the integration asks for:

1. **Bambu Lab account**: email and password, then an email code or a 2FA code when Bambu Lab asks for one. The password is only used to get a sign-in token and is never stored.
2. **Google Drive**: the service account JSON and the folder.

Afterwards:

- **Configure** (options): how often to poll the cloud and the folder, color matching, and notifications.
- **Reconfigure**: sign in to Bambu Lab again, or change the Drive folder or key. Leave the JSON empty to keep the stored key.

The Bambu Lab token lasts about 90 days. Two weeks before it expires a repair issue (and, if enabled, a notification) asks you to sign in again. If the cloud rejects the token, Home Assistant starts the sign-in flow.

## Entities

One device **Filament Stock** with these sensors.

Per color group, for example `PLA #006633 matte`:

| Entity | Meaning |
| --- | --- |
| `… stock` (number) | Remaining grams over all spools of the group, minus local usage. You can change it in Home Assistant, see below. |
| `… needed` (sensor) | Grams needed by the sliced files in the queue. |
| `… balance` (sensor) | Stock minus needed. Negative means you need more filament. |

Attributes: `material`, `color`, `note`, `spools` (id, name, vendor, remaining, in printer) and `file_colors` (which file colors count here and how they were matched).

**Changing the stock in Home Assistant.** Enter a new value in the stock number, for example after weighing a spool. It is stored as a local correction for the color group (attribute `correction`), prints are deducted from it, and the balance follows. When the remaining weight of a spool of that group changes in Bambu Studio, the cloud value wins again. The value is not written to the Bambu Lab cloud.

A file color without any matching spool gets its own group `unknown …` with a negative balance. It is never ignored.

Color group entities are created and removed with the groups. A group that disappears is removed from the entity registry, it is never shown as 0 g.

General sensors:

| Sensor | Meaning |
| --- | --- |
| Plates in queue | Number of sliced plates, with the files as attributes. |
| Total needed | Grams needed for the whole queue. |
| Total stock | Grams remaining over all spools. |
| Not sliced | Files in the folder without sliced plates (diagnostic). |
| Unmatched colors | File colors without a color group (diagnostic). |
| Last deduction | Result of the last tracked print, with file, plate, share and grams per color. |

### Dashboard example

```yaml
type: entities
title: Filament
entities:
  - sensor.filament_stock_total_stock
  - sensor.filament_stock_total_needed
  - sensor.filament_stock_plates_in_queue
  - sensor.filament_stock_last_deduction
```

Entity IDs depend on your colors and language. Check them under **Settings > Devices & services > Filament Stock**.

## Actions

| Action | Use |
| --- | --- |
| `filament_stock.refresh` | Read the cloud and the folder now. |
| `filament_stock.map_color` | Always count a file color (`material`, `color`) against a chosen color group (any of its entities). Leave out `group` to remove the mapping. Admin only. |
| `filament_stock.reset_ledger` | Forget the local usage of one spool (`spool_id`) or of a whole color group, and its correction. Admin only. |

## Color matching

The color in a sliced file is the color of the filament profile in your project. It is not always exactly the color of a spool in your list. Each file color `(material, #RRGGBB)` is matched like this:

1. A **fixed choice**: the select `… in files` that Filament Stock adds for every color in the queue (or the action `filament_stock.map_color`).
2. A group with the **same material and color**. If there are several (same color, different note), the group with a spool in the printer wins, else the group with the most stock. The choice is shown in the `file_colors` attribute.
3. Optional (on by default): the **nearest color** of the same material within a maximum distance. The distance is [CIEDE2000](https://en.wikipedia.org/wiki/Color_difference#CIEDE2000), which follows how people see color differences. About 2 is barely visible, 10 (the default) is a clearly similar shade. Example: a project color `#101410` (near black) matches a black spool.
4. **No match**: the color gets an `unknown` group with a negative balance, the **Unmatched colors** sensor, a repair issue and a notification. The notification names the nearest spool in stock, or tells you to add the spool in Bambu Studio > Filament Manager with the exact material and color code.

**Choosing in Home Assistant.** Bambu Studio only lets you slice with filaments in your AMS. For a plate you will print later with another filament, pick the right spool in the select `<color> in files` (for example `PLA #898989 in files` > `PLA #FF3300 Army Red`). It stays `Automatic` otherwise. The attributes show which files use the color, how it is counted now and the nearest spool.

**When a print is booked**, the spools that are in the printer (`inPrinter` in the Bambu Lab cloud) are tried first: the nearest loaded spool of the same material within the maximum distance gets the filament. With three greys in stock and one grey in the AMS, the loaded grey is booked. A fixed choice still wins.

## Deduction rules

The print is followed through the `print_status` sensor of ha-bambulab.

| Printer status | What happens |
| --- | --- |
| `prepare`, `slicing`, `running`, `pause` | A job starts. The file, the plate and the grams per color are stored, so a restart or moving the file does not lose the deduction. |
| `finish` | 100 % of the plate is deducted. |
| `failed` (also a cancelled print) | The share reached while `running` is deducted. |
| `idle`, `init` without `finish` or `failed` | Nothing is deducted. |
| `offline`, `unknown`, `unavailable` | The job is kept, the printer may just be gone for a moment. |

- The printed file is found by the `subtask_name` of the printer, compared to the file names (lower case, without `.gcode.3mf`, `.3mf` or `.gcode`, letters and digits only). Files that left the folder are remembered.
- The plate number comes from the printer file name (`plate_2.gcode`). If it cannot be found, the total of the file is used and the **Last deduction** sensor says so (`whole_file`).
- Within a color group, the spool in the printer is used first, then the spool with the least remaining, so opened spools get used up. When a group runs empty, the rest is booked as a shortage.
- Every print is deducted once. The same end reported twice does nothing.

## Notifications

Under **Configure** choose how to be notified:

- **WhatsApp Notifier** (recommended): free, sends WhatsApp messages straight from Home Assistant, without an external server or paid API. It uses an unofficial WhatsApp protocol, so use it with that in mind. Leave the chat ID empty for its default chat.
- **Any notify entity**: a WhatsApp Notifier chat, the mobile app, and so on.
- **Home Assistant notification**: a persistent notification. Also used when sending fails.

Each kind can be switched on or off: new spool found (with a reminder to check its weight), balance below 0 g, balance below a threshold, unknown color in the queue (with the nearest spool or the color code to add in Bambu Studio), unsliced file in the folder, Bambu Lab sign-in expires soon.

Each event is sent once. A balance alert comes back only after the balance recovered and drops again. Events within 30 seconds are combined, and at most 20 messages per minute are sent. Texts follow the language of Home Assistant (English and Dutch).

## Limitations

- The Bambu Lab cloud API is unofficial and may change without notice. Only the global region is supported.
- Writing usage back to the Bambu Lab cloud is not available yet. Usage lives in the local ledger.
- If the cloud ever returns an empty spool list while you had spools, the update is ignored to protect the ledger. If you really remove every spool, remove and add the integration.
- If a print ends while Home Assistant is down and you then print the same file again, the two prints can count as one.
- Print tracking needs ha-bambulab. Without it, stock and balance still work, but nothing is deducted.

## Troubleshooting

| Problem | Cause and fix |
| --- | --- |
| "The Google Drive API is not enabled" | Enable the Drive API for the project of the service account (step 2). |
| "Folder not found" or "Access denied" | Share the folder with the service account email as Viewer, and check the folder ID. A folder that is not shared looks empty to Google, so the integration checks the folder itself first. |
| "This is not valid JSON" | Paste the complete file, including the first and last curly bracket. |
| Creating a key is blocked | Your organization blocks service account keys (`iam.disableServiceAccountKeyCreation`). Use a personal Google account or ask the admin. |
| A file is listed as "Not sliced" | Export it with **Export plate sliced file**, see above. |
| "Wrong email or password" | Use the Bambu Lab account email and password, not a third-party login. |
| "The Bambu Lab cloud blocked the request" | Cloudflare blocked the request. Try again later. |
| A color stays "unmatched" | Add the spool in Bambu Studio, raise the maximum color distance, or use `filament_stock.map_color`. |
| Last deduction says "No matching file" | The printer file name does not match any file in the folder. Keep the name of the sliced file the same as the print job. |

Diagnostics (**⋮ > Download diagnostics**) hide your token, account, Google key, folder ID and printer serials.

## Privacy and security

- The Bambu Lab password is never stored. The token, the account email and the Google service account key are stored in the config entry.
- Home Assistant stores config entries in `.storage` **without encryption**. Anyone with access to your Home Assistant files or backups can read the service account key. It only has read access to the folders you share with it. Revoke the key in the Google Cloud Console if it ever leaks.
- Google access is read-only. Bambu Lab access is read-only.
- Only `api.bambulab.com`, `bambulab.com` and Google API hosts are contacted.
- File contents are treated as untrusted: sizes are limited, zip files are read in memory only and XML with a DTD is rejected.

## Credits

- [Timmes123/ha-bambu-filaments](https://github.com/Timmes123/ha-bambu-filaments) for documenting the Bambu Lab cloud filament API and login flow. This integration has its own implementation.
- [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab) for the printer integration that print tracking builds on.
- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) for the community documentation of the Bambu Lab cloud.

Not affiliated with or endorsed by Bambu Lab or Google.

## License

MIT, see [LICENSE](LICENSE).
