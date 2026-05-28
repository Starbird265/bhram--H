use std::process::Command;
use std::sync::Mutex;
use tauri::Manager;

/// State to track the backend process so we can kill it on exit.
struct BackendProcess(Mutex<Option<std::process::Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // ── Logging (debug only) ────────────────────────────
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // ── Auto-start Python backend ───────────────────────
            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));

            // Resolve backend directory (works in dev and bundled)
            let backend_dir = if resource_dir.join("backend").exists() {
                resource_dir.join("backend")
            } else {
                // Dev mode: go up from src-tauri
                let dev_path = std::env::current_dir()
                    .unwrap_or_default()
                    .parent()
                    .map(|p| p.join("backend"))
                    .unwrap_or_else(|| std::path::PathBuf::from("../backend"));
                dev_path
            };

            let backend_src = backend_dir.join("src");

            log::info!("Starting Python backend from: {:?}", backend_src);

            // Bug 3 fix: use the venv python so all packages (slack_sdk, mcp, etc.) are available.
            // Falls back to global python3 only if venv doesn't exist (CI / first install).
            let venv_python = backend_dir.join("venv").join("bin").join("python");
            let python_exe = if venv_python.exists() {
                venv_python
            } else {
                log::warn!("venv not found at {:?}, using global python3", venv_python);
                std::path::PathBuf::from("python3")
            };

            log::info!("Using Python: {:?}", python_exe);

            let child = Command::new(&python_exe)
                .arg("-m")
                .arg("uvicorn")
                .arg("api:app")
                .arg("--host")
                .arg("127.0.0.1")
                .arg("--port")
                .arg("8000")
                .current_dir(&backend_src)
                .env("PYTHONPATH", &backend_src)
                .spawn();

            match child {
                Ok(process) => {
                    log::info!("Backend started (PID: {})", process.id());
                    app.manage(BackendProcess(Mutex::new(Some(process))));
                }
                Err(e) => {
                    log::error!("Failed to start backend: {}", e);
                    // Still manage empty state so the on_exit handler works
                    app.manage(BackendProcess(Mutex::new(None)));
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            // Kill the backend when the window is destroyed
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(ref mut child) = *guard {
                            log::info!("Stopping backend (PID: {})", child.id());
                            let _ = child.kill();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Cortex AI");
}
