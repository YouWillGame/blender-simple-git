# Simple Git Commit for Blender

[Русская версия](README.ru.md)

A small single-file Blender add-on that lets you work with Git without leaving Blender: commit, push, pull, branches, switching between commits, cloning and first-time repository setup with Git LFS.

> The interface is currently in Russian only.

## Features

- **Commit / Push / Pull** from the 3D Viewport side panel (`N` → **Git**). Push, Pull and Clone run in the background and do not freeze Blender.
- **Branches**: list, switch, create, rename.
- **Commits**: pick one of the last 50 commits and check it out (detached HEAD), then create a branch to continue from there.
- **Remote**: set or update the `origin` URL (GitHub, GitLab, self-hosted).
- **Init**: `git init` with the branch name you choose (no more `master`), optionally moving the open `.blend` into a subfolder.
- **Clone** a repository into a folder you pick.
- **Setup**: creates or extends `.gitignore` and `.gitattributes` from a built-in base (backups like `*.blend1` ignored, `.blend`, textures and common 3D formats tracked with Git LFS), runs `git lfs install`, and removes already tracked `.blend1`/`.blend2` backups from the index. Extra patterns can be added in the add-on preferences.

## Requirements

- Blender 4.2 or newer (developed on Blender 5.3).
- [Git](https://git-scm.com/) available in `PATH`.
- [Git LFS](https://git-lfs.com/) is strongly recommended for `.blend` projects.
- For private repositories, sign in once from a terminal or Git Credential Manager. Blender cannot answer password prompts.

## Installation

1. Download `git_commit_addon.py` from the [latest release](../../releases/latest).
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk** (in Blender 4.2+ it is in the drop-down at the top right) and pick the file.
3. Enable **Simple Git Commit**.
4. In the 3D Viewport press `N` and open the **Git** tab.

## Quick start

1. Save your `.blend` file.
2. Open **Новый репозиторий** (New repository) → **Init репозиторий**, or **Клонировать** to clone an existing one.
3. Press **Setup** to create `.gitignore` / `.gitattributes` and enable LFS.
4. Open **Удалённый репозиторий** (Remote), paste the repository URL and press **Привязать** (Link).
5. Type a message, press **Commit**, then **Push**.

## Working with `.blend` files

`.blend` is a binary format: Git cannot diff or merge it, so two people must not edit the same file at the same time. Split the project into several files (Link / Library Overrides) and consider Git LFS file locks (`lockable` in `.gitattributes`).

## Limitations

- No merge, conflict resolution, per-file staging or visual history graph (`git add -A` is used).
- Committing runs synchronously, so a very large commit can keep Blender busy for a while.
- After Pull or switching branch/commit, re-open the `.blend` if it changed on disk.

## License

GPL-3.0-or-later.
