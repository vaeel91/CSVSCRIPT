#!/usr/bin/env python3
"""
Phone Number Search Tool - GUI (Tkinter)
Interfaccia grafica per phone_search.py
Supporta drag & drop di file CSV/VCF.
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


class PhoneSearchGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Phone Number Search Tool v2.0")
        self.root.geometry("900x700")
        self.root.configure(bg="#f5f6fa")

        self.selected_file = tk.StringVar(value="")
        self.is_running = False

        self._build_ui()

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg="#667eea", height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Phone Number Search Tool v2.0",
                 font=("Segoe UI", 16, "bold"), bg="#667eea", fg="white").pack(pady=15)

        # Main frame
        main = tk.Frame(self.root, bg="#f5f6fa", padx=20, pady=15)
        main.pack(fill="both", expand=True)

        # File selection
        file_frame = tk.LabelFrame(main, text="File Rubrica", font=("Segoe UI", 10, "bold"),
                                   bg="#f5f6fa", padx=10, pady=10)
        file_frame.pack(fill="x", pady=(0, 10))

        tk.Entry(file_frame, textvariable=self.selected_file, font=("Segoe UI", 10),
                 width=60).pack(side="left", padx=(0, 10))
        tk.Button(file_frame, text="Sfoglia...", command=self._browse_file,
                  font=("Segoe UI", 9), bg="#667eea", fg="white",
                  relief="flat", padx=15, pady=5).pack(side="left")

        # Options
        opts_frame = tk.LabelFrame(main, text="Opzioni", font=("Segoe UI", 10, "bold"),
                                   bg="#f5f6fa", padx=10, pady=10)
        opts_frame.pack(fill="x", pady=(0, 10))

        row1 = tk.Frame(opts_frame, bg="#f5f6fa")
        row1.pack(fill="x", pady=3)

        tk.Label(row1, text="Thread:", bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")
        self.workers_var = tk.StringVar(value="5")
        tk.Spinbox(row1, from_=1, to=20, textvariable=self.workers_var,
                   width=5, font=("Segoe UI", 9)).pack(side="left", padx=(5, 20))

        tk.Label(row1, text="Delay (sec):", bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")
        self.delay_var = tk.StringVar(value="2.0")
        tk.Spinbox(row1, from_=0.5, to=30, increment=0.5, textvariable=self.delay_var,
                   width=5, font=("Segoe UI", 9)).pack(side="left", padx=(5, 20))

        tk.Label(row1, text="Limite:", bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")
        self.limit_var = tk.StringVar(value="0")
        tk.Spinbox(row1, from_=0, to=10000, textvariable=self.limit_var,
                   width=6, font=("Segoe UI", 9)).pack(side="left", padx=5)

        row2 = tk.Frame(opts_frame, bg="#f5f6fa")
        row2.pack(fill="x", pady=3)

        tk.Label(row2, text="Motori:", bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")
        self.engines_var = tk.StringVar(value="google,bing,duckduckgo")
        tk.Entry(row2, textvariable=self.engines_var, font=("Segoe UI", 9),
                 width=40).pack(side="left", padx=5)

        row3 = tk.Frame(opts_frame, bg="#f5f6fa")
        row3.pack(fill="x", pady=3)

        self.resume_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="Resume (riprendi)", variable=self.resume_var,
                       bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")

        self.monitor_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="Monitoraggio", variable=self.monitor_var,
                       bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left", padx=15)

        self.nocache_var = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="No cache", variable=self.nocache_var,
                       bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left")

        self.html_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row3, text="Report HTML", variable=self.html_var,
                       bg="#f5f6fa", font=("Segoe UI", 9)).pack(side="left", padx=15)

        # Buttons
        btn_frame = tk.Frame(main, bg="#f5f6fa")
        btn_frame.pack(fill="x", pady=(0, 10))

        self.start_btn = tk.Button(
            btn_frame, text="AVVIA SCANSIONE", command=self._start_scan,
            font=("Segoe UI", 11, "bold"), bg="#2ecc71", fg="white",
            relief="flat", padx=30, pady=8,
        )
        self.start_btn.pack(side="left")

        self.stop_btn = tk.Button(
            btn_frame, text="STOP", command=self._stop_scan, state="disabled",
            font=("Segoe UI", 11, "bold"), bg="#e74c3c", fg="white",
            relief="flat", padx=30, pady=8,
        )
        self.stop_btn.pack(side="left", padx=10)

        # Progress
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                            maximum=100, length=400)
        self.progress_bar.pack(fill="x", pady=(0, 5))

        self.status_label = tk.Label(main, text="Pronto", bg="#f5f6fa",
                                     font=("Segoe UI", 9), fg="#7f8c8d")
        self.status_label.pack(anchor="w")

        # Output log
        log_frame = tk.LabelFrame(main, text="Output", font=("Segoe UI", 10, "bold"),
                                  bg="#f5f6fa")
        log_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, font=("Consolas", 9), bg="#2c3e50", fg="#ecf0f1",
            insertbackground="white", wrap="word",
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    def _browse_file(self):
        filepath = filedialog.askopenfilename(
            title="Seleziona rubrica",
            filetypes=[
                ("Tutti i supportati", "*.csv *.vcf *.txt"),
                ("CSV", "*.csv"),
                ("vCard", "*.vcf"),
                ("Testo", "*.txt"),
            ],
        )
        if filepath:
            self.selected_file.set(filepath)

    def _log(self, text: str):
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def _start_scan(self):
        filepath = self.selected_file.get()
        if not filepath or not os.path.isfile(filepath):
            messagebox.showerror("Errore", "Seleziona un file rubrica valido!")
            return

        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log_text.delete("1.0", "end")

        thread = threading.Thread(target=self._run_scan, daemon=True)
        thread.start()

    def _stop_scan(self):
        self.is_running = False
        self.status_label.configure(text="Interrotto dall'utente")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _run_scan(self):
        filepath = self.selected_file.get()
        cmd_parts = [sys.executable, "phone_search.py", filepath]
        cmd_parts += ["--workers", self.workers_var.get()]
        cmd_parts += ["--delay", self.delay_var.get()]
        cmd_parts += ["--engines", self.engines_var.get()]

        limit = self.limit_var.get()
        if limit and limit != "0":
            cmd_parts += ["--limit", limit]

        if self.resume_var.get():
            cmd_parts.append("--resume")
        if self.monitor_var.get():
            cmd_parts.append("--monitor")
        if self.nocache_var.get():
            cmd_parts.append("--no-cache")
        if self.html_var.get():
            html_name = os.path.splitext(os.path.basename(filepath))[0] + "_report.html"
            cmd_parts += ["--html", html_name]

        self.root.after(0, lambda: self._log(f"Comando: {' '.join(cmd_parts)}\n"))
        self.root.after(0, lambda: self.status_label.configure(text="Scansione in corso..."))

        import subprocess
        try:
            process = subprocess.Popen(
                cmd_parts, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            for line in process.stdout:
                if not self.is_running:
                    process.terminate()
                    break
                self.root.after(0, lambda l=line: self._log(l.rstrip()))

            process.wait()
            self.root.after(0, lambda: self.status_label.configure(text="Completato!"))
            self.root.after(0, lambda: self._log("\n✅ Scansione completata!"))

        except Exception as e:
            self.root.after(0, lambda: self._log(f"\n❌ Errore: {e}"))
            self.root.after(0, lambda: self.status_label.configure(text=f"Errore: {e}"))

        self.root.after(0, lambda: self.start_btn.configure(state="normal"))
        self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
        self.is_running = False

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = PhoneSearchGUI()
    app.run()
