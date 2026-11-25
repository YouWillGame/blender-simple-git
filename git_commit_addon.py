bl_info = {
    "name": "Simple Git Commit",
    "author": "YouWillGame",
    "version": (1, 5, 0),
    "blender": (4, 2, 0),
    "doc_url": "https://github.com/YouWillGame/blender-simple-git",
    "tracker_url": "https://github.com/YouWillGame/blender-simple-git/issues",
    "location": "3D Viewport > N panel > Git",
    "description": "Init, clone, branches, commits, setup, pull and push from Blender",
    "category": "System",
}

import os
import re
import subprocess
import threading

import bpy

# ---------------------------------------------------------------------------
# Константы. Недостающие строки аддон дописывает в .gitignore / .gitattributes,
# существующие не трогает и не дублирует.
# ---------------------------------------------------------------------------

BASE_IGNORE = [
    "*.blend1", "*.blend2", "*.blend@",
    "__pycache__/", "*.pyc", "*.tmp", ".DS_Store", "Thumbs.db",
]

BASE_LFS = [
    "*.blend",
    "*.png", "*.jpg", "*.jpeg", "*.tga", "*.tif", "*.tiff", "*.psd",
    "*.exr", "*.hdr",
    "*.fbx", "*.gltf", "*.glb", "*.obj", "*.abc", "*.usd", "*.usdc", "*.usdz",
    "*.mp4", "*.mov", "*.wav",
]

LFS_SUFFIX = " filter=lfs diff=lfs merge=lfs -text"

REMOTE_URL_RE = re.compile(r"^(https?://|ssh://|git@)\S+$")
URL_WITH_CREDENTIALS_RE = re.compile(r"^https?://[^/@\s]+@")

NO_REPO = "Git не найден или .blend не сохранён внутри репозитория (создайте его кнопкой Init)"

# Состояние для панели: в draw() git не запускаем.
_branch_cache = []                       # [(id, name, description)] для списка веток
_commit_cache = []                       # то же для списка коммитов
_state = {"branch": "", "head": ""}      # текущая ветка и короткий хэш коммита
_job = {"running": False, "message": ""}  # фоновая задача (clone / push / pull)


class GitFail(Exception):
    """Ожидаемая ошибка: текст показывается пользователю, оператор отменяется."""

    def __init__(self, msg, level='ERROR'):
        super().__init__(msg)
        self.level = level


# ---------------------------------------------------------------------------
# Настройки аддона
# ---------------------------------------------------------------------------

def _addon_key():
    return __package__ or __name__


class GitAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = _addon_key()

    extra_ignore: bpy.props.StringProperty(
        name="Доп. правила .gitignore",
        description="Через запятую, например: Exports/, *.log")
    extra_lfs: bpy.props.StringProperty(
        name="Доп. форматы для LFS",
        description="Через запятую, например: *.ma, *.zip")

    def draw(self, context):
        col = self.layout.column()
        col.label(text="Дополнительно к базовому набору (через запятую):")
        col.prop(self, "extra_ignore")
        col.prop(self, "extra_lfs")


def _extra(attr):
    addon = bpy.context.preferences.addons.get(_addon_key())
    text = getattr(addon.preferences, attr, "") if addon else ""
    return [p.strip() for p in text.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git(cwd, *args, timeout=60):
    """git без окна консоли и без ожидания пароля; вывод всегда в UTF-8."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _reason(text, default=""):
    """Самая информативная строка вывода: первая error/fatal, иначе последняя."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if line.startswith(("fatal:", "error:")):
            return line
    return lines[-1] if lines else default


def _must(cwd, *args, err="Ошибка git", timeout=60):
    """Выполнить git, при ошибке бросить GitFail. Возвращает stdout."""
    r = _git(cwd, *args, timeout=timeout)
    if r.returncode != 0:
        raise GitFail(_reason(r.stderr + "\n" + r.stdout, err))
    return r.stdout.strip()


def _repo_root():
    if not bpy.data.filepath:
        return None
    try:
        r = _git(os.path.dirname(bpy.data.filepath), "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _has_commits(repo):
    return _git(repo, "rev-parse", "-q", "--verify", "HEAD", timeout=10).returncode == 0


def _current_branch(repo):
    """Имя текущей ветки (работает и в пустом репозитории); "" при detached HEAD."""
    return _git(repo, "symbolic-ref", "-q", "--short", "HEAD", timeout=10).stdout.strip()


def _remote(repo):
    r = _git(repo, "remote", "get-url", "origin", timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def _check_branch_name(cwd, name):
    if not name or _git(cwd, "check-ref-format", "--branch", name, timeout=10).returncode != 0:
        raise GitFail("Недопустимое имя ветки: " + (name or "(пусто)"))


def _check_url(url):
    if not url:
        raise GitFail("Вставьте ссылку на репозиторий")
    if not REMOTE_URL_RE.match(url):
        raise GitFail("Ссылка должна начинаться с https://, ssh:// или git@")
    if URL_WITH_CREDENTIALS_RE.match(url):
        raise GitFail("Не вставляйте логин, пароль или токен в ссылку: "
                      "они сохранятся в .git/config открытым текстом")


def _set_enum(scene, prop, value):
    try:
        setattr(scene, prop, value)
    except (TypeError, AttributeError):
        pass


def _refresh_branches(scene):
    """Обновить кэши веток и коммитов (3 вызова git). Возвращает корень репозитория или None."""
    global _branch_cache, _commit_cache
    _branch_cache, _commit_cache = [], []
    _state.update(branch="", head="")

    repo = _repo_root()
    if not repo:
        return None

    names, cur = [], ""
    out = _git(repo, "for-each-ref", "--format=%(HEAD)%(refname:short)", "refs/heads", timeout=10).stdout
    for line in out.splitlines():
        if line.strip():
            names.append(line[1:].strip())
            if line[0] == "*":
                cur = names[-1]
    if not cur:
        cur = _current_branch(repo)  # ветка без коммитов; при detached HEAD пусто
        if cur and cur not in names:
            names.insert(0, cur)

    head = _git(repo, "rev-parse", "--short", "-q", "--verify", "HEAD", timeout=10).stdout.strip()
    _state.update(branch=cur, head=head)
    _branch_cache = [(n, n, "") for n in names]
    _set_enum(scene, "git_branch", cur)

    if head:
        log = _git(repo, "log", "--branches", "HEAD", "-n", "50", "--format=%h%x09%s", timeout=15).stdout
        for line in log.splitlines():
            h, _, subj = line.partition("\t")
            if h.strip():
                mark = "\u25cf " if h == head else ""
                _commit_cache.append((h, "%s%s  %s" % (mark, h, subj[:45]), subj))
        _set_enum(scene, "git_commit_pick", head)
    return repo


def _branch_items(self, context):
    return _branch_cache or [("NONE", "(нет веток)", "")]


def _commit_items(self, context):
    return _commit_cache or [("NONE", "(нет коммитов)", "")]


def _append_missing(path, wanted, key=lambda line: line):
    """Дописать в файл строки из `wanted`, которых там ещё нет. Возвращает добавленные."""
    text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    have = {key(l.strip()) for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")}
    added = [w for w in wanted if key(w) not in have]
    if added:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("\n".join(added) + "\n")
    return added


# ---------------------------------------------------------------------------
# Фоновые задачи (clone / push / pull не блокируют Blender)
# ---------------------------------------------------------------------------

def _redraw_all():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            area.tag_redraw()


def _job_poll():
    if _job["running"]:
        return 0.5
    try:
        _refresh_branches(bpy.context.scene)
        _redraw_all()
    except Exception:
        pass
    return None


def _start_job(title, func):
    """func() выполняется в потоке и возвращает текст результата. bpy внутри не трогать."""
    _job.update(running=True, message=title + "...")

    def worker():
        try:
            msg = func()
        except GitFail as e:
            msg = "Ошибка: %s" % e
        except FileNotFoundError:
            msg = "Ошибка: git не найден в PATH"
        except subprocess.TimeoutExpired:
            msg = "Ошибка: %s, таймаут" % title
        except OSError as e:
            msg = "Ошибка: %s" % e
        _job.update(running=False, message=msg)

    threading.Thread(target=worker, daemon=True).start()
    bpy.app.timers.register(_job_poll, first_interval=0.5)


def _push_job(repo):
    def work():
        _must(repo, "push", "-u", "origin", "HEAD", timeout=900)
        return "Push выполнен"
    return work


def _pull_job(repo):
    def work():
        out = _must(repo, "pull", timeout=900)
        return _reason(out, "Pull выполнен") + " (если .blend изменился, откройте его заново)"
    return work


# ---------------------------------------------------------------------------
# Базовый оператор: проверка репозитория, перехват ошибок, отчёты
# ---------------------------------------------------------------------------

class GitOperator(bpy.types.Operator):
    uses_repo = True  # False для операторов, которым репозиторий ещё не нужен

    def run(self, context, repo):
        """Вернуть текст успеха (или None). Ошибки: raise GitFail(...)."""
        raise NotImplementedError

    def execute(self, context):
        try:
            repo = None
            if self.uses_repo:
                if _job["running"]:
                    raise GitFail("Дождитесь завершения: " + _job["message"], 'WARNING')
                repo = _repo_root()
                if not repo:
                    raise GitFail(NO_REPO)
            msg = self.run(context, repo)
        except GitFail as e:
            self.report({e.level}, str(e))
            return {'CANCELLED'}
        except FileNotFoundError:
            self.report({'ERROR'}, "git не найден в PATH")
            return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, "git завис (таймаут)")
            return {'CANCELLED'}
        except OSError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if msg:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Репозиторий: init / clone
# ---------------------------------------------------------------------------

class GIT_OT_init(GitOperator):
    bl_idname = "wm.git_init"
    bl_label = "Init репозиторий"
    bl_description = ("git init в папке открытого .blend с веткой из поля «Имя ветки». "
                      "Файл можно сразу переместить в подпапку (по умолчанию Blend)")
    uses_repo = False

    move_to_subfolder: bpy.props.BoolProperty(
        name="Переместить .blend в подпапку",
        description="После создания репозитория перенести открытый файл в подпапку",
        default=True)
    subfolder: bpy.props.StringProperty(name="Подпапка", default="Blend")

    def invoke(self, context, event):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "Сначала сохраните .blend файл")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        folder, filename = os.path.split(bpy.data.filepath)
        sub = self.subfolder.strip()
        layout.label(text="Репозиторий будет создан в папке:")
        layout.label(text=folder)
        layout.label(text="Ветка: " + (context.scene.git_new_branch.strip() or "main"))
        layout.separator()
        layout.prop(self, "move_to_subfolder")
        row = layout.row()
        row.enabled = self.move_to_subfolder
        row.prop(self, "subfolder")
        if self.move_to_subfolder and sub:
            layout.label(text="Файл будет: " + os.path.join(folder, sub, filename))

    def _target(self, folder, filename):
        """Новый путь файла или "" если переносить не нужно."""
        sub = self.subfolder.strip()
        if not (self.move_to_subfolder and sub):
            return ""
        if re.search(r'[\\/:*?"<>|]', sub) or sub in (".", ".."):
            raise GitFail("Недопустимое имя подпапки: " + sub)
        if os.path.basename(folder).lower() == sub.lower():
            return ""
        path = os.path.join(folder, sub, filename)
        if os.path.exists(path):
            raise GitFail("Файл уже существует: " + path)
        return path

    def run(self, context, _repo):
        old = bpy.data.filepath
        if not old:
            raise GitFail("Сначала сохраните .blend файл")
        if _repo_root():
            raise GitFail("Файл уже лежит внутри git-репозитория")
        folder, filename = os.path.split(old)
        branch = context.scene.git_new_branch.strip() or "main"
        _check_branch_name(folder, branch)
        new_path = self._target(folder, filename)

        if _git(folder, "init", "-b", branch, timeout=30).returncode != 0:
            # git старее 2.28 не знает -b
            _must(folder, "init", timeout=30)
            _must(folder, "symbolic-ref", "HEAD", "refs/heads/" + branch, timeout=10)

        moved = ""
        if new_path:
            try:
                os.makedirs(os.path.dirname(new_path), exist_ok=True)
                # Сохраняем под новым путём (относительные пути пересчитываются), старый файл удаляем
                bpy.ops.wm.save_as_mainfile(filepath=new_path)
                moved = new_path
                os.remove(old)
            except (OSError, RuntimeError) as e:
                self.report({'WARNING'}, "Перенос файла не завершён: %s" % e)

        _refresh_branches(context.scene)
        msg = "Репозиторий создан, ветка: " + branch
        if moved:
            msg += ". Файл теперь: " + moved
        return msg + ". Дальше: Setup"


class GIT_OT_clone(GitOperator):
    bl_idname = "wm.git_clone"
    bl_label = "Клонировать"
    bl_description = "git clone в выбранную папку (в фоне, Blender не блокируется)"
    uses_repo = False

    def run(self, context, _repo):
        sc = context.scene
        url = sc.git_clone_url.strip()
        _check_url(url)
        if _job["running"]:
            raise GitFail("Дождитесь завершения: " + _job["message"], 'WARNING')

        if not sc.git_clone_dir.strip():
            raise GitFail("Выберите папку, куда клонировать")
        parent = os.path.normpath(bpy.path.abspath(sc.git_clone_dir.strip()))
        name = re.split(r"[/:]", url.rstrip("/"))[-1]
        name = name[:-4] if name.endswith(".git") else name
        if not name:
            raise GitFail("Не удалось определить имя репозитория из ссылки")
        target = os.path.join(parent, name)
        if os.path.isdir(target) and os.listdir(target):
            raise GitFail("Папка уже существует и не пуста: " + target)

        def work():
            os.makedirs(parent, exist_ok=True)
            _git(parent, "lfs", "install", "--skip-repo", timeout=30)
            _must(parent, "clone", url, target, timeout=3600, err="clone не удался")
            return "Клонировано: " + target

        _start_job("Клонирование", work)
        return "Клонирование запущено в фоне"


# ---------------------------------------------------------------------------
# Ветки и коммиты
# ---------------------------------------------------------------------------

class GIT_OT_refresh_branches(GitOperator):
    bl_idname = "wm.git_refresh_branches"
    bl_label = ""
    bl_description = "Обновить списки веток и коммитов"
    uses_repo = False

    def run(self, context, _repo):
        if not _refresh_branches(context.scene):
            raise GitFail(NO_REPO)


class GIT_OT_switch_branch(GitOperator):
    bl_idname = "wm.git_switch_branch"
    bl_label = "Переключить"
    bl_description = "git switch на выбранную ветку (лучше сначала сделать Commit)"

    def run(self, context, repo):
        target = context.scene.git_branch
        if target in ("", "NONE"):
            raise GitFail("Ветка не выбрана. Нажмите кнопку обновления списка")
        if target == _state["branch"]:
            raise GitFail("Уже на ветке " + target, 'INFO')
        _must(repo, "switch", target, err="Не удалось переключиться", timeout=120)
        _refresh_branches(context.scene)
        return "Ветка: %s (если .blend изменился, откройте его заново)" % target


class GIT_OT_create_branch(GitOperator):
    bl_idname = "wm.git_create_branch"
    bl_label = "Создать"
    bl_description = "Создать новую ветку из текущего коммита и перейти на неё (git switch -c)"

    def run(self, context, repo):
        name = context.scene.git_new_branch.strip()
        _check_branch_name(repo, name)
        if not _has_commits(repo):
            raise GitFail("В пустом репозитории нет коммитов: сначала сделайте Commit или переименуйте ветку")
        _must(repo, "switch", "-c", name, err="Не удалось создать ветку", timeout=30)
        _refresh_branches(context.scene)
        return "Создана ветка %s, вы на ней" % name


class GIT_OT_rename_branch(GitOperator):
    bl_idname = "wm.git_rename_branch"
    bl_label = "Переименовать"
    bl_description = "Переименовать текущую ветку в имя из поля (git branch -m)"

    def run(self, context, repo):
        new = context.scene.git_new_branch.strip()
        _check_branch_name(repo, new)
        _refresh_branches(context.scene)
        cur = _state["branch"]
        if not cur:
            raise GitFail("Сейчас нет текущей ветки (detached HEAD)")
        if new == cur:
            raise GitFail("Ветка уже называется " + new, 'INFO')
        if _has_commits(repo):
            _must(repo, "branch", "-m", new, err="Не удалось переименовать", timeout=10)
        else:  # в пустом репозитории ветки ещё нет, меняем только HEAD
            _must(repo, "symbolic-ref", "HEAD", "refs/heads/" + new, timeout=10)
        _refresh_branches(context.scene)
        return "Ветка %s переименована в %s. Дальше: Push" % (cur, new)


class GIT_OT_checkout_commit(GitOperator):
    bl_idname = "wm.git_checkout_commit"
    bl_label = "Перейти на коммит"
    bl_description = ("git switch --detach: посмотреть проект на выбранном коммите. Чтобы продолжить "
                      "работу отсюда, создайте ветку; чтобы вернуться, переключитесь на ветку")

    def run(self, context, repo):
        target = context.scene.git_commit_pick
        if target in ("", "NONE"):
            raise GitFail("Коммит не выбран. Нажмите кнопку обновления списка")
        if target == _state["head"]:
            raise GitFail("Вы уже на коммите " + target, 'INFO')
        _must(repo, "switch", "--detach", target, err="Не удалось перейти на коммит", timeout=120)
        _refresh_branches(context.scene)
        return ("Вы на коммите %s (detached HEAD). Откройте .blend заново. "
                "Создайте ветку, чтобы коммитить отсюда" % target)


# ---------------------------------------------------------------------------
# Remote, setup, commit, push, pull
# ---------------------------------------------------------------------------

class GIT_OT_set_remote(GitOperator):
    bl_idname = "wm.git_set_remote"
    bl_label = "Привязать"
    bl_description = "Указать ссылку на удалённый репозиторий (GitHub, GitLab) как origin"

    def run(self, context, repo):
        url = context.scene.git_remote_url.strip()
        _check_url(url)
        cmd = "set-url" if _remote(repo) else "add"
        _must(repo, "remote", cmd, "origin", url, err="Не удалось изменить remote", timeout=10)
        return "Remote origin %s: %s" % ("обновлён" if cmd == "set-url" else "добавлен", url)


class GIT_OT_get_remote(GitOperator):
    bl_idname = "wm.git_get_remote"
    bl_label = ""
    bl_description = "Прочитать текущую ссылку origin из репозитория"

    def run(self, context, repo):
        url = _remote(repo)
        if not url:
            raise GitFail("Remote origin ещё не задан", 'WARNING')
        context.scene.git_remote_url = url


class GIT_OT_setup(GitOperator):
    bl_idname = "wm.git_setup"
    bl_label = "Setup (.gitignore + LFS)"
    bl_description = ("Создать или дополнить .gitignore и .gitattributes из базового набора, включить "
                      "Git LFS и убрать из индекса бэкапы .blend1/.blend2")

    def run(self, context, repo):
        ignore = BASE_IGNORE + [p for p in _extra("extra_ignore") if p not in BASE_IGNORE]
        lfs = BASE_LFS + [p for p in _extra("extra_lfs") if p not in BASE_LFS]

        added_ignore = _append_missing(os.path.join(repo, ".gitignore"), ignore)
        added_lfs = _append_missing(
            os.path.join(repo, ".gitattributes"),
            [p + LFS_SUFFIX for p in lfs],
            key=lambda line: line.split()[0] if line.split() else line)

        lfs_ok = _git(repo, "lfs", "install", "--local").returncode == 0
        tracked = [l for l in _git(repo, "ls-files", "--", "*.blend1", "*.blend2", "*.blend@")
                   .stdout.splitlines() if l.strip()]
        if tracked:
            _must(repo, "rm", "--cached", "-q", "--", *tracked)

        parts = [".gitignore +%d" % len(added_ignore), ".gitattributes +%d" % len(added_lfs)]
        if tracked:
            parts.append("убрано из индекса бэкапов: %d" % len(tracked))
        if not lfs_ok:
            self.report({'WARNING'}, "Git LFS не установлен (git lfs install не прошёл). " + ", ".join(parts))
            return None
        return "Готово: " + ", ".join(parts) + ". Теперь сделайте Commit."


class GIT_OT_commit(GitOperator):
    bl_idname = "wm.git_commit"
    bl_label = "Commit"
    bl_description = "Сохранить файл, git add -A и git commit"

    def run(self, context, repo):
        if not _current_branch(repo) and _has_commits(repo):
            raise GitFail("Вы на отдельном коммите (detached HEAD). Создайте ветку, чтобы коммитить")

        bpy.ops.wm.save_mainfile()
        sc = context.scene
        msg = sc.git_message.strip() or "Update"

        _git(repo, "add", "-A", timeout=600)  # LFS-фильтр больших файлов может работать долго
        r = _git(repo, "commit", "-m", msg, timeout=600)
        if r.returncode != 0:
            raise GitFail(_reason(r.stdout + "\n" + r.stderr, "Нечего коммитить"), 'WARNING')
        _refresh_branches(sc)

        if sc.git_push:
            _start_job("Push", _push_job(repo))
            return "Коммит создан: %s. Push запущен в фоне" % msg
        return "Коммит создан: " + msg


class GIT_OT_push(GitOperator):
    bl_idname = "wm.git_push"
    bl_label = "Push"
    bl_description = "git push -u origin HEAD в фоне (отправить текущую ветку)"

    def run(self, context, repo):
        _start_job("Push", _push_job(repo))
        return "Push запущен в фоне"


class GIT_OT_pull(GitOperator):
    bl_idname = "wm.git_pull"
    bl_label = "Pull"
    bl_description = "git pull в фоне (сохраните и закройте работу заранее)"

    def run(self, context, repo):
        _start_job("Pull", _pull_job(repo))
        return "Pull запущен в фоне"


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------

class GIT_PT_panel(bpy.types.Panel):
    bl_label = "Git"
    bl_idname = "GIT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Git"

    def draw(self, context):
        sc = context.scene
        layout = self.layout
        col = layout.column(align=True)
        col.prop(sc, "git_message", text="", placeholder="Сообщение коммита")
        col.prop(sc, "git_push")
        col.operator("wm.git_commit", icon='CHECKMARK')
        row = layout.row(align=True)
        row.operator("wm.git_pull", icon='IMPORT')
        row.operator("wm.git_push", icon='EXPORT')
        if _job["message"]:
            layout.label(text=_job["message"], icon='TIME' if _job["running"] else 'INFO')


class _SubPanel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_parent_id = "GIT_PT_panel"


class GIT_PT_branches(_SubPanel):
    bl_label = "Ветки и коммиты"

    def draw(self, context):
        sc = context.scene
        layout = self.layout
        cur, head = _state["branch"], _state["head"]
        if cur:
            layout.label(text="Ветка: " + cur, icon='OUTLINER_DATA_GP_LAYER')
        elif head:
            layout.label(text="Detached HEAD (" + head + ")", icon='ERROR')
        else:
            layout.label(text="Ветка: (не определена)", icon='OUTLINER_DATA_GP_LAYER')

        row = layout.row(align=True)
        row.prop(sc, "git_branch", text="")
        row.operator("wm.git_refresh_branches", icon='FILE_REFRESH')
        layout.operator("wm.git_switch_branch", icon='LOOP_FORWARDS')

        layout.separator()
        layout.prop(sc, "git_new_branch", text="", placeholder="Имя ветки, например main")
        row = layout.row(align=True)
        row.operator("wm.git_create_branch", icon='ADD')
        row.operator("wm.git_rename_branch", icon='GREASEPENCIL')

        layout.separator()
        layout.label(text="Коммит: " + (head or "нет"))
        layout.prop(sc, "git_commit_pick", text="")
        layout.operator("wm.git_checkout_commit", icon='RECOVER_LAST')


class GIT_PT_remote(_SubPanel):
    bl_label = "Удалённый репозиторий"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        row = self.layout.row(align=True)
        row.prop(context.scene, "git_remote_url", text="",
                 placeholder="Ссылка: https://gitlab.com/user/repo.git")
        row.operator("wm.git_get_remote", icon='FILE_REFRESH')
        self.layout.operator("wm.git_set_remote", icon='LINKED')


class GIT_PT_new_repo(_SubPanel):
    bl_label = "Новый репозиторий"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sc = context.scene
        layout = self.layout
        layout.operator("wm.git_init", icon='FILE_NEW')
        layout.separator()
        layout.prop(sc, "git_clone_url", text="", placeholder="Ссылка для клонирования (https://...)")
        layout.prop(sc, "git_clone_dir", text="", placeholder="Папка, куда клонировать")
        row = layout.row()
        row.enabled = not _job["running"]
        row.operator("wm.git_clone", icon='IMPORT')


class GIT_PT_setup(_SubPanel):
    bl_label = "Настройка"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        self.layout.operator("wm.git_setup", icon='PREFERENCES')


classes = (
    GitAddonPreferences,
    GIT_OT_init, GIT_OT_clone,
    GIT_OT_refresh_branches, GIT_OT_switch_branch, GIT_OT_create_branch,
    GIT_OT_rename_branch, GIT_OT_checkout_commit,
    GIT_OT_set_remote, GIT_OT_get_remote, GIT_OT_setup,
    GIT_OT_commit, GIT_OT_push, GIT_OT_pull,
    GIT_PT_panel, GIT_PT_branches, GIT_PT_remote, GIT_PT_new_repo, GIT_PT_setup,
)

SCENE_PROPS = {
    "git_message": bpy.props.StringProperty(
        name="Сообщение", description="Сообщение коммита (если пусто, будет «Update»)"),
    "git_push": bpy.props.BoolProperty(
        name="Сразу push", description="Отправить на сервер после коммита"),
    "git_remote_url": bpy.props.StringProperty(
        name="Remote URL", description="Ссылка на репозиторий, например https://gitlab.com/user/project.git"),
    "git_branch": bpy.props.EnumProperty(
        name="Ветка", description="Локальные ветки репозитория", items=_branch_items),
    "git_commit_pick": bpy.props.EnumProperty(
        name="Коммит", description="Последние коммиты (● отмечает текущий)", items=_commit_items),
    "git_new_branch": bpy.props.StringProperty(
        name="Имя ветки", description="Имя для создания или переименования ветки и для Init"),
    "git_clone_url": bpy.props.StringProperty(
        name="Clone URL", description="Ссылка для клонирования, например https://github.com/user/project.git"),
    "git_clone_dir": bpy.props.StringProperty(
        name="Папка", description="Папка, внутри которой появится папка репозитория", subtype='DIR_PATH'),
}


def register():
    for name, prop in SCENE_PROPS.items():
        setattr(bpy.types.Scene, name, prop)
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    for name in SCENE_PROPS:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)


if __name__ == "__main__":
    register()
