NeoMatrix 1.4.11 — BOOX reading data in Calibre

INSTALL
Install the plugin ZIP in Calibre: Preferences > Plugins > Load plugin from file.
Restart Calibre. Install Android Platform Tools (ADB), enable USB debugging on
BOOX, and approve the computer on the device. The first Sync may create BOOX
columns and ask for one Calibre restart. BOOX may separately ask you to approve
an Android backup when collecting handwriting metadata.

EVERYDAY USE
Save and close books in both readers. Connect and unlock BOOX, then click Sync.
NeoMatrix reads the device, imports changed notes and reading data, and compares
reading positions in both places. Up-to-date books are not rewritten.

When farther Calibre positions can safely be sent to BOOX, Sync shows the book
names and asks: Sync both ways, Read BOOX only, or Cancel. No position is sent
without that choice. Backups are saved automatically on your computer before
any device update. Reopen and close each pushed book in NeoReader afterward;
this loads the new location and refreshes the BOOX page percentage.

Farther Calibre positions are kept even if a push is declined or unsupported.
If both readers changed since the last pull, the position is kept and the
conflict is reported. No position is guessed from a percentage.

WHAT WORKS
- BOOX to Calibre: supported EPUB highlights, typed notes, bookmarks and verified
  resume locations become native Calibre reader data. Reading progress uses
  Calibre's standard 0–1 fraction and the BOOX percentage column.
- Calibre to BOOX: on Android 12 or newer, verified EPUB text locations can be
  sent even when first reached on another reader. Existing mapped BOOX locations
  remain supported. New locations require the same EPUB reading content and a
  source-position convention verified by independent BOOX text locations.
  Unverified, image/spatial, or unsupported locations are skipped with a reason.
  Highlights, typed notes and handwriting are not pushed to BOOX yet.
- PDF, CBZ and CBR: two-way progress uses verified physical pages in the matching
  Calibre format. The farther page wins regardless of timestamps. Original notes
  remain archived; supported physical-page bookmarks are imported.
- Original BOOX annotations, metadata and handwriting files are archived locally.
  Handwriting can be viewed in NeoMatrix; native editable handwriting overlays
  are not implemented.

SAFETY AND COMPATIBILITY
Push identifies the exact BOOX copy by document ID, record ID and path. Separate
volumes stay separate. EPUB reading resources and structure must match the
verified Calibre archive. Calibre bookmarks, bibliographic metadata, ZIP packing
and manifest order may differ; text, images, styles, reading order, manifest IDs
and layout metadata must match. Encrypted packages retain all package metadata.
The exact BOOX file checksum is checked again before update or restore;
BOOX's metadata MD5 tag is only an identity value.
A changed file, changed device state or ambiguous position stops the push.
The original metadata is backed up first, the update is conditional, and device
readback must match. A repeat push to the same location performs no write.

A pushed book cannot import an old BOOX page fraction before NeoReader refreshes
it. Ordinary imports retain unrelated annotations and farther Calibre positions.
No ebook file is modified. Only the exported Android metadata provider is used;
no rooting, firmware modification or proprietary source code is required.

MORE
Saved reading data: browse local reading and handwriting archives.
BOOX cleanup: Book records shows current files, old locations and possible moved
or replacement copies. Selected records can be excluded from sync and included
again. Exclusions are local to this computer and the exact unchanged record on
that BOOX. Reading again, moving or replacing a record makes it eligible again.
Finishing a book or organizing it on a shelf never automatically excludes it.
Unused files reviews verified orphaned reader files. Scan makes no changes.
Removal requires a separate choice and a verified backup.
Books still present on internal memory/SD cards, Kindle CR! downloads, tiny caches,
shared databases and uncertain data are protected. Cleanup does not run in Sync.
Sync skips confirmed missing paths while keeping saved history, reports incomplete
file checks, and separates current books from archives in Sync details.
Restore a reading-position backup: restores the old position only on the same
BOOX and exact book, provided there has been no newer reading since the push.
Advanced: manual imports, position overrides, reports and local diagnosis export.
Diagnosis exports contain private book/reading data; nothing is uploaded by the
plugin. The release ZIP excludes device snapshots, books, backups and diagnostics.

VALIDATION
Version 1.4.2 preserves NeoReader document-root and whitespace paths separately
from Calibre CFI paths. Observed highlight quotes verify the path convention
before resume conversion; missing or inconsistent evidence remains unsupported.

Version 1.4.1 was checked against actual Calibre and BOOX copies of two EPUBs
whose only differences were metadata and Calibre bookmarks. Reading resources,
chapter identities and converted anchors matched. A third EPUB with a changed
image remained blocked. Fourteen regression tests cover these comparisons,
incremental archive checks and checksum guards for push, backup and restore.

The first device push was checked on an original synthetic two-chapter EPUB on
BOOX Go Color 7 Gen II / Android 13. NeoReader reopened at the pushed first chapter
and changed its progress from 11/20 to 1/20. Repeat push was unchanged; other books,
bookmarks, annotations and EPUB bytes were unchanged. Firmware/format support
varies, so unsupported positions are reported and retained.

Developer regression tests cover incremental writes, separate volumes, duplicate
copies, missing paths, cleanup protection, position mapping, content checks,
backups, conditional updates, restore guards, and the main Sync choices.

NeoMatrix is an independent compatibility tool. No BOOX firmware, third-party
books or device-private diagnostics are distributed with it.

MAPPING UPDATES
After a reading-position mapper update, Sync rechecks existing EPUB archives
once even when BOOX data has not changed. Remaining unsupported positions are
listed in the sync summary, including on repeat syncs. No page turn is needed.

PROGRESS CONFLICTS
Automatic sync retains the farthest verified reading location across BOOX and all
Calibre last-read sources, regardless of timestamps. Uncomparable locations are
preserved and reported. Tools > Use BOOX reading position is an explicit override.
The EPUB CFI resolver is shared with the CrossPoint SD plugin in this repository
family so both plugins compare the same spine/text ordering.

1.4.5: Verify additional BOOX document-root path conventions using each book's
highlight quotes. Sync details distinguish image-only cover/title-page starts,
BOOX PDF versus Calibre EPUB copies, and unresolved text locations. Existing
positions are preserved whenever a safe comparison is unavailable.

BOOX cleanup: scan status and an animated activity bar stay visible above both tabs.
Select all checks only available rows on the current tab; Clear selection unchecks them.
Use the action buttons below the list to exclude selected records from sync or back up and remove selected unused files.
Scanning does not require a sync first.

Book records: Remove selected from BOOX saves a verified JSON backup and deletes only selected stale Metadata entries. Current books are protected. Notes, bookmarks, handwriting, shelf tables and ebook files are not deleted; NeoReader may hide the removed entries/history until Restore record backup is used. Moved/unchecked entries need review; a missing old path is not a reading-position failure.
Record removal is limited to the verified NeoReader provider build (SHA-256 539b26bdb3ffecd91a22a0dbe753af43c0e11f65f61d1de851c4516d65500b13). Other firmware is blocked until its deletion and insertion behavior is verified. Normal Sync never deletes records.

1.4.8 reads BOOX metadata without trimming spaces or losing newlines/literal NULL text. Sync and cleanup use the same values. Partial-cleanup messages identify the stopped record and count removals; restore can recognize untouched entries in older trimmed backups without modifying them.

1.4.9 adds exact source-verified reverse EPUB position conversion. It verifies
chapter order and cumulative resource sizes against native chapter bounds,
requires a uniquely calibrated raw DOM convention and two independent native
text nodes, and round-trips the generated position to the identical Calibre CFI.
No percentage or rendered-text offset is substituted for a NeoReader source
coordinate. Source encodings, surrogate character conventions and DOM layouts
that cannot be verified are rejected. BOOX positions in earlier verified
chapters can be compared without guessing an offset inside a blank element.
The Grimscribe X3-to-BOOX handoff was verified on a physical GoColor7_2, followed
by a cold-open/close and native position readback; notes and bookmarks remained
unchanged. Repeat preview reported unchanged. Older device clocks do not defeat
farthest-position selection. The CrossPoint Calibre plugin handles EPUB only.

1.4.10 adds two-way PDF, CBZ and CBR physical-page progress. Sync checks both
files, native page references and Calibre viewer page markers before moving a
position. Different formats are not interchangeable. PDF page numbering stays
physical even if the Calibre conversion omits blank pages; positions on omitted
pages are reported as unsupported. Printed page labels may differ from counters.
PDF smoothness and geometry rounding introduced by NeoReader is normalized
narrowly; page content, fonts, images and reading order must still match.
Comic ordering is restricted to the verified NeoReader build and unambiguous
single-folder numeric page names. Other layouts/firmware are reported and kept.
PDF parsing is bundled (pypdf 6.10.0, BSD-3-Clause); CBR uses Calibre's RAR support.
No additional plugin or paid service is required. Device writes retain the
existing explicit review, verified backup and conditional update protections.
Hardware cold-open tests confirmed PDF page21, CBR page10 and CBZ page9. Isolated
Calibre runtime tests cover both directions and newer-but-earlier positions.

1.4.11 gives background workers an explicit 8 MiB stack. Calibre's native RAR
reader exceeds the macOS Qt default stack; real worker-thread CBR regression
tests now cover that path rather than testing only on the main thread.
