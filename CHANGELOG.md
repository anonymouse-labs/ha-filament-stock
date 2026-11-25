# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Repository skeleton, license, CI and release workflow.
- Parser for sliced `.gcode.3mf` files with size limits and safe XML handling.
- Color groups by material, color and note; matching with CIEDE2000.
- Spool ledger with rebase on cloud weight change and duplicate protection.
- Read-only Google Drive client (service account) with separate error causes.
- Print queue coordinator with a stored cache: startup never waits on downloads.
- Read-only Bambu Lab cloud client: login with email code or 2FA, paginated spool list.
- Spool coordinator with a stored list, token expiry warning in Repairs and reauth.
- Config flow: Bambu Lab account, then the Drive folder; reconfigure and options.
- Diagnostics with redaction of tokens, accounts, keys and device serials.
- Sensors per color group (stock, needed, balance) and for queue totals, added and removed with the groups.
- Actions `refresh`, `map_color` and `reset_ledger`; options for nearest color matching.
- Print tracking via ha-bambulab: the file and grams per color are stored at print start; finish books 100 %, failed books the progress reached, each print is booked once. Last deduction sensor.
- Notifications for new spools, negative or low balance, unsliced files and token expiry; via WhatsApp Notifier, any notify entity or a persistent notification. Each event once, combined and rate limited.
- Repair issues for unsliced files and file colors without a spool.
- Stock per color group is a number entity that can be changed in Home Assistant, as a local correction until the cloud weight changes.
- Select per color in the queue to choose the spool it counts against; notification for unknown colors with the nearest spool or the color code to add in Bambu Studio.
- Booking a print prefers the spool that is in the printer.
- Last deduction lists colors that match two loaded spools equally (`ambiguous`). README section on linking spools to the AMS.
- Own brand icon, README with setup guide, security policy, contributing guide and issue templates.
