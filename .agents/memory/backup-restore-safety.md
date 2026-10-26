---
name: Backup restore safety
description: User-directed default behavior for restoring server backups.
---

When restoring a server backup, preserve roles and channels added after the snapshot by default. Offer an explicit option to delete newer roles and channels, and require an administrator confirmation before carrying out that destructive restore.

**Why:** the user requested a keep-or-delete option with preservation as the default.

**How to apply:** maintain this default in any changes to backup restore flows; deletion must remain explicit and confirmation-gated.
