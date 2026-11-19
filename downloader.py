import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk
import yt_dlp


class DownloadCancelled(Exception):
    """Исключение для корректной остановки загрузки пользователем."""
    pass


class YTDLPLogger:
    """Перенаправляет сообщения yt-dlp (включая пропуски из архива) в окно логов."""
    def __init__(self, log_callback, skip_callback=None):
        self.log_callback = log_callback
        self.skip_callback = skip_callback

    def debug(self, msg):
        if "has already been recorded in the archive" in msg:
            clean = (
                msg.replace("[download]", "")
                .replace("has already been recorded in the archive", "")
                .strip(" :")
            )
            self.log_callback(f"⏭ Пропущено (уже в архиве): {clean}")
            if self.skip_callback:
                self.skip_callback()

    def warning(self, msg):
        lower = msg.lower()
        if any(k in lower for k in ("ffmpeg", "private", "login", "cookies", "unable", "429")):
            self.log_callback(f"[Предупреждение] {msg}")

    def error(self, msg):
        self.log_callback(f"[Ошибка] {msg}")


def sanitize_folder_name(name: str) -> str:
    """Убирает из названия плейлиста символы, запрещённые в именах папок."""
    cleaned = re.sub(r'[\\/*?:"<>|]+', "_", name).strip(". ")
    return cleaned or "Playlist"


def get_app_data_dir() -> str:
    """Возвращает системную папку с правами записи (безопасно для .app и .exe)."""
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")

    app_dir = os.path.join(base, "MediaDownloader")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def find_ffmpeg() -> Optional[str]:
    """Ищет FFmpeg внутри собранного .app/.exe, рядом с приложением и в системе."""
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, exe_name))

    exec_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates.append(os.path.join(exec_dir, exe_name))

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, exe_name))

    which_path = shutil.which("ffmpeg")
    if which_path:
        candidates.append(which_path)

    if sys.platform == "darwin":
        candidates.extend([
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        ])
    elif os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates.extend([
            r"C:\ffmpeg\bin\ffmpeg.exe",
            os.path.join(local_app, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        ])
    else:
        candidates.extend(["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.dirname(candidate)
    return None


def open_in_os(path: str):
    """Открывает файл или папку стандартным средством ОС."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


class MediaDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("Media Downloader — YouTube / TikTok / Instagram / SoundCloud")
        self.geometry("820x780")
        self.minsize(720, 700)

        self.is_downloading = False
        self.cancel_event = threading.Event()
        self.skipped_in_batch = 0

        self.ffmpeg_dir = find_ffmpeg()
        self.app_data_dir = get_app_data_dir()
        self.archive_path = os.path.join(self.app_data_dir, "download_archive.txt")
        self.default_output_dir = os.path.join(os.path.expanduser("~"), "Downloads", "MediaDownloader")

        self._build_ui()
        self._update_mode_visibility()
        self._refresh_archive_count()

        if not self.ffmpeg_dir:
            self.log(
                "⚠ FFmpeg не найден! Конвертация в MP3, склейка видео высокого качества "
                "и вшивание обложек не будут работать, пока FFmpeg не установлен или не встроен в сборку."
            )

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # --- Блок ссылок ---
        url_header = ctk.CTkFrame(self, fg_color="transparent")
        url_header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        url_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            url_header,
            text="Ссылки (YouTube, TikTok, Instagram, SoundCloud — по одной на строку):",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            url_header, text="Вставить из буфера", width=140, height=28,
            command=self._paste_from_clipboard,
        ).grid(row=0, column=1, padx=(8, 4))

        ctk.CTkButton(
            url_header, text="Очистить", width=85, height=28,
            fg_color="#555555", hover_color="#444444",
            command=self._clear_urls,
        ).grid(row=0, column=2)

        self.url_textbox = ctk.CTkTextbox(self, height=115, font=ctk.CTkFont(size=12))
        self.url_textbox.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="ew")

        # --- Панель настроек ---
        settings_frame = ctk.CTkFrame(self)
        settings_frame.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        settings_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Режим: Видео / Только аудио
        ctk.CTkLabel(settings_frame, text="Режим скачивания:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=(10, 4), sticky="w"
        )
        self.mode_var = ctk.StringVar(value="Видео")
        self.mode_selector = ctk.CTkSegmentedButton(
            settings_frame,
            values=["Видео", "Только аудио"],
            variable=self.mode_var,
            command=lambda _: self._update_mode_visibility(),
        )
        self.mode_selector.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")

        # Настройки видео
        self.video_fmt_label = ctk.CTkLabel(settings_frame, text="Формат видео:")
        self.video_fmt_var = ctk.StringVar(value="mp4")
        self.video_fmt_menu = ctk.CTkOptionMenu(
            settings_frame, values=["mp4", "mkv", "webm"], variable=self.video_fmt_var
        )

        self.video_qual_label = ctk.CTkLabel(settings_frame, text="Качество видео:")
        self.video_qual_var = ctk.StringVar(value="Максимум")
        self.video_qual_menu = ctk.CTkOptionMenu(
            settings_frame,
            values=["Максимум", "1080p", "720p", "480p", "360p"],
            variable=self.video_qual_var,
        )

        # Настройки аудио
        self.audio_fmt_label = ctk.CTkLabel(settings_frame, text="Формат аудио:")
        self.audio_fmt_var = ctk.StringVar(value="mp3")
        self.audio_fmt_menu = ctk.CTkOptionMenu(
            settings_frame, values=["mp3", "m4a", "flac", "wav"], variable=self.audio_fmt_var
        )

        self.audio_qual_label = ctk.CTkLabel(settings_frame, text="Битрейт аудио:")
        self.audio_qual_var = ctk.StringVar(value="320 kbps")
        self.audio_qual_menu = ctk.CTkOptionMenu(
            settings_frame, values=["320 kbps", "192 kbps", "128 kbps"], variable=self.audio_qual_var
        )

        # Выбор потоков фрагментов (всегда видим в 4-й колонке)
        ctk.CTkLabel(settings_frame, text="Потоки фрагментов:").grid(
            row=0, column=3, padx=12, pady=(10, 4), sticky="w"
        )
        self.threads_var = ctk.StringVar(value="4")
        self.threads_menu = ctk.CTkOptionMenu(
            settings_frame,
            values=["1", "4", "8"],
            variable=self.threads_var,
        )
        self.threads_menu.grid(row=1, column=3, padx=12, pady=(0, 10), sticky="ew")

        # Чекбоксы основных опций
        checks_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        checks_frame.grid(row=2, column=0, columnspan=4, padx=12, pady=(0, 6), sticky="ew")

        self.playlist_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            checks_frame,
            text="Скачивать плейлисты целиком (в папку с названием плейлиста)",
            variable=self.playlist_var,
        ).pack(side="left", padx=(0, 20))

        self.metadata_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            checks_frame,
            text="Обложка и теги (для музыки)",
            variable=self.metadata_var,
        ).pack(side="left")

        # Строка архива (пропуск дубликатов)
        archive_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        archive_frame.grid(row=3, column=0, columnspan=4, padx=12, pady=(2, 10), sticky="ew")
        archive_frame.grid_columnconfigure(1, weight=1)

        self.archive_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            archive_frame,
            text="Пропускать уже скачанное (архив дубликатов)",
            variable=self.archive_var,
        ).grid(row=0, column=0, sticky="w")

        self.archive_count_label = ctk.CTkLabel(
            archive_frame, text="В архиве: 0", text_color="gray", anchor="e"
        )
        self.archive_count_label.grid(row=0, column=1, padx=10, sticky="ew")

        ctk.CTkButton(
            archive_frame, text="Открыть архив", width=110, height=26,
            fg_color="#555555", hover_color="#444444",
            command=self._open_archive_file,
        ).grid(row=0, column=2, padx=(0, 6))

        ctk.CTkButton(
            archive_frame, text="Очистить архив", width=115, height=26,
            fg_color="#7a3b3b", hover_color="#632e2e",
            command=self._clear_archive,
        ).grid(row=0, column=3)

        # --- Папка сохранения ---
        folder_frame = ctk.CTkFrame(self, fg_color="transparent")
        folder_frame.grid(row=3, column=0, padx=16, pady=6, sticky="ew")
        folder_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(folder_frame, text="Папка:").grid(row=0, column=0, padx=(0, 8))
        self.folder_entry = ctk.CTkEntry(folder_frame)
        self.folder_entry.insert(0, self.default_output_dir)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        ctk.CTkButton(folder_frame, text="Обзор...", width=90, command=self._choose_folder).grid(
            row=0, column=2, padx=(0, 6)
        )
        ctk.CTkButton(
            folder_frame, text="Открыть", width=85, fg_color="#555555", hover_color="#444444",
            command=self._open_folder,
        ).grid(row=0, column=3)

        # --- Кнопки управления и прогресс ---
        controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        controls_frame.grid(row=4, column=0, padx=16, pady=8, sticky="ew")
        controls_frame.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(controls_frame, fg_color="transparent")
        btn_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        btn_row.grid_columnconfigure(0, weight=3)
        btn_row.grid_columnconfigure(1, weight=1)

        self.download_btn = ctk.CTkButton(
            btn_row, text="Скачать всё", height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_download,
        )
        self.download_btn.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        self.cancel_btn = ctk.CTkButton(
            btn_row, text="Отмена", height=40,
            fg_color="#b33939", hover_color="#8f2929",
            state="disabled", command=self._cancel_download,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(controls_frame)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        self.status_label = ctk.CTkLabel(controls_frame, text="Готов к работе", anchor="w")
        self.status_label.grid(row=2, column=0, sticky="ew")

        # --- Лог ---
        self.log_box = ctk.CTkTextbox(self, state="disabled", font=ctk.CTkFont(family="Courier", size=12))
        self.log_box.grid(row=5, column=0, padx=16, pady=(4, 16), sticky="nsew")

    def _update_mode_visibility(self):
        is_video = self.mode_var.get() == "Видео"
        if is_video:
            self.audio_fmt_label.grid_remove()
            self.audio_fmt_menu.grid_remove()
            self.audio_qual_label.grid_remove()
            self.audio_qual_menu.grid_remove()

            self.video_fmt_label.grid(row=0, column=1, padx=12, pady=(10, 4), sticky="w")
            self.video_fmt_menu.grid(row=1, column=1, padx=12, pady=(0, 10), sticky="ew")
            self.video_qual_label.grid(row=0, column=2, padx=12, pady=(10, 4), sticky="w")
            self.video_qual_menu.grid(row=1, column=2, padx=12, pady=(0, 10), sticky="ew")
        else:
            self.video_fmt_label.grid_remove()
            self.video_fmt_menu.grid_remove()
            self.video_qual_label.grid_remove()
            self.video_qual_menu.grid_remove()

            self.audio_fmt_label.grid(row=0, column=1, padx=12, pady=(10, 4), sticky="w")
            self.audio_fmt_menu.grid(row=1, column=1, padx=12, pady=(0, 10), sticky="ew")
            self.audio_qual_label.grid(row=0, column=2, padx=12, pady=(10, 4), sticky="w")
            self.audio_qual_menu.grid(row=1, column=2, padx=12, pady=(0, 10), sticky="ew")

    def _count_archive_entries(self) -> int:
        if not os.path.isfile(self.archive_path):
            return 0
        try:
            with open(self.archive_path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _refresh_archive_count(self):
        count = self._count_archive_entries()
        self.archive_count_label.configure(text=f"В архиве: {count}")

    def _open_archive_file(self):
        if not os.path.isfile(self.archive_path):
            with open(self.archive_path, "a", encoding="utf-8"):
                pass
        open_in_os(self.archive_path)

    def _clear_archive(self):
        count = self._count_archive_entries()
        if count == 0:
            messagebox.showinfo("Архив пуст", "В архиве пока нет ни одной записи.")
            return
        confirm = messagebox.askyesno(
            "Очистить архив?",
            f"Удалить все записи из архива ({count} шт.)?\n"
            "Уже скачанные файлы на диске останутся, но при повторной вставке ссылок программа скачает их заново.",
        )
        if confirm:
            try:
                open(self.archive_path, "w", encoding="utf-8").close()
                self._refresh_archive_count()
                self.log("Архив скачанного очищен.")
            except OSError as err:
                messagebox.showerror("Ошибка", f"Не удалось очистить архив: {err}")

    def _paste_from_clipboard(self):
        try:
            text = self.clipboard_get().strip()
            if text:
                current = self.url_textbox.get("1.0", "end").strip()
                prefix = "\n" if current else ""
                self.url_textbox.insert("end", f"{prefix}{text}\n")
        except Exception:
            pass

    def _clear_urls(self):
        self.url_textbox.delete("1.0", "end")

    def _choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.folder_entry.get())
        if selected:
            self.folder_entry.delete(0, "end")
            self.folder_entry.insert(0, selected)

    def _open_folder(self):
        path = self.folder_entry.get().strip()
        os.makedirs(path, exist_ok=True)
        open_in_os(path)

    def log(self, message: str):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _append)

    def set_status(self, text: str, progress: Optional[float] = None):
        def _update():
            self.status_label.configure(text=text)
            if progress is not None:
                self.progress_bar.set(max(0.0, min(1.0, progress)))
        self.after(0, _update)

    def _on_item_skipped(self):
        self.skipped_in_batch += 1

    def _start_download(self):
        if self.is_downloading:
            return

        raw_text = self.url_textbox.get("1.0", "end")
        urls = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not urls:
            messagebox.showwarning("Нет ссылок", "Вставьте хотя бы одну ссылку.")
            return

        output_dir = self.folder_entry.get().strip() or self.default_output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.is_downloading = True
        self.skipped_in_batch = 0
        self.cancel_event.clear()
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)

        config = {
            "mode": self.mode_var.get(),
            "video_fmt": self.video_fmt_var.get(),
            "video_qual": self.video_qual_var.get(),
            "audio_fmt": self.audio_fmt_var.get(),
            "audio_qual": self.audio_qual_var.get().split()[0],
            "threads": int(self.threads_var.get()),
            "allow_playlist": self.playlist_var.get(),
            "embed_meta": self.metadata_var.get(),
            "use_archive": self.archive_var.get(),
            "output_dir": output_dir,
        }

        threading.Thread(target=self._download_worker, args=(urls, config), daemon=True).start()

    def _cancel_download(self):
        if self.is_downloading:
            self.cancel_event.set()
            self.set_status("Остановка загрузки...")
            self.log("Запрошена отмена загрузки...")

    def _detect_playlist_title(self, url: str) -> Optional[str]:
        """Проверяет, является ли ссылка плейлистом, и возвращает его название."""
        probe_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and (info.get("_type") == "playlist" or "entries" in info):
                    title = info.get("title") or info.get("playlist_title")
                    if title:
                        return sanitize_folder_name(title)
        except Exception:
            pass
        return None

    def _build_ydl_opts(self, config: dict, playlist_folder: Optional[str] = None) -> dict:
        base_dir = config["output_dir"]
        if playlist_folder and config["allow_playlist"]:
            target_dir = os.path.join(base_dir, playlist_folder)
            os.makedirs(target_dir, exist_ok=True)
            outtmpl = os.path.join(target_dir, "%(playlist_index)02d - %(title)s.%(ext)s")
        else:
            outtmpl = os.path.join(base_dir, "%(title)s.%(ext)s")

        opts = {
            "outtmpl": outtmpl,
            "noplaylist": not config["allow_playlist"],
            "ignoreerrors": "only_download",
            "windowsfilenames": True,
            "concurrent_fragment_downloads": config["threads"],
            "http_chunk_size": 10485760,
            "quiet": True,
            "no_warnings": False,
            "logger": YTDLPLogger(self.log, self._on_item_skipped),
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
        }

        if config["use_archive"]:
            opts["download_archive"] = self.archive_path

        if self.ffmpeg_dir:
            opts["ffmpeg_location"] = self.ffmpeg_dir

        if config["mode"] == "Только аудио":
            audio_fmt = config["audio_fmt"]
            bitrate = config["audio_qual"]

            opts["format"] = "bestaudio/best"
            postprocessors = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_fmt,
                    "preferredquality": bitrate,
                }
            ]

            if config["embed_meta"] and self.ffmpeg_dir:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
                if audio_fmt in ("mp3", "m4a", "flac"):
                    opts["writethumbnail"] = True
                    postprocessors.append({"key": "EmbedThumbnail"})

            opts["postprocessors"] = postprocessors
        else:
            v_fmt = config["video_fmt"]
            qual = config["video_qual"]

            if qual == "Максимум":
                fmt_str = "bestvideo+bestaudio/best"
                format_sort = [f"ext:{v_fmt}:m4a", "vcodec:h264", "acodec:aac"] if v_fmt == "mp4" else []
            else:
                height = qual.replace("p", "")
                fmt_str = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
                format_sort = (
                    [f"res:{height}", f"ext:{v_fmt}:m4a", "vcodec:h264", "acodec:aac"]
                    if v_fmt == "mp4"
                    else [f"res:{height}"]
                )

            opts["format"] = fmt_str
            if format_sort:
                opts["format_sort"] = format_sort
            opts["merge_output_format"] = v_fmt

        return opts

    def _progress_hook(self, d: dict):
        if self.cancel_event.is_set():
            raise DownloadCancelled()

        status = d.get("status")
        if status == "downloading":
            filename = os.path.basename(d.get("filename", ""))
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            ratio = (downloaded / total) if total > 0 else 0.0
            speed = d.get("speed")
            speed_str = f"{speed / (1024 * 1024):.1f} МБ/с" if speed else "..."
            self.set_status(f"Скачивание: {filename} ({ratio * 100:.1f}% | {speed_str})", ratio)
        elif status == "finished":
            filename = os.path.basename(d.get("filename", ""))
            self.log(f"✓ Загружено: {filename}")
            self.set_status(f"Обработка (FFmpeg): {filename}...", 1.0)

    def _postprocessor_hook(self, d: dict):
        if self.cancel_event.is_set():
            raise DownloadCancelled()
        if d.get("status") == "finished" and d.get("postprocessor") == "MoveFiles":
            self.set_status("Файл готов", 1.0)

    def _download_worker(self, urls: list[str], config: dict):
        total_urls = len(urls)
        success_count = 0

        archive_status = "вкл" if config["use_archive"] else "выкл"
        self.log(
            f"--- Старт: ссылок {total_urls} | Режим: {config['mode']} | "
            f"Потоки: {config['threads']} | Архив: {archive_status} ---"
        )

        try:
            for idx, url in enumerate(urls, start=1):
                if self.cancel_event.is_set():
                    raise DownloadCancelled()

                self.log(f"[{idx}/{total_urls}] Обработка: {url}")
                self.set_status(f"[{idx}/{total_urls}] Проверка ссылки...", 0.0)

                playlist_folder = None
                if config["allow_playlist"]:
                    playlist_folder = self._detect_playlist_title(url)
                    if playlist_folder:
                        self.log(f"📁 Плейлист: создаём папку «{playlist_folder}»")

                opts = self._build_ydl_opts(config, playlist_folder=playlist_folder)
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                    success_count += 1
                except DownloadCancelled:
                    raise
                except Exception as err:
                    self.log(f"✗ Не удалось скачать {url}: {err}")

            skip_note = f" | Пропущено дубликатов: {self.skipped_in_batch}" if self.skipped_in_batch else ""
            self.set_status(f"Готово! Обработано ссылок: {success_count}/{total_urls}{skip_note}", 1.0)
            self.log(f"--- Завершено ({success_count}/{total_urls}){skip_note} ---\n")

        except DownloadCancelled:
            self.set_status("Загрузка отменена пользователем", 0.0)
            self.log("--- Отменено ---\n")
        finally:
            self.is_downloading = False
            self.after(0, self._refresh_archive_count)
            self.after(0, lambda: self.download_btn.configure(state="normal"))
            self.after(0, lambda: self.cancel_btn.configure(state="disabled"))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = MediaDownloaderApp()
    app.mainloop()