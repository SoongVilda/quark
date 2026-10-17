// Logging macros: [HH:MM:SS] [LEVEL]   message
// Uses libc::localtime_r for local time without pulling in chrono.
//
// Diagnostics (timing, deploy detail) are gated behind QUARK_VERBOSE=1.
// Use log_verbose!() for these.

use std::sync::OnceLock;

/// Every log, trace and incident file quark writes lives here.
pub const LOG_DIR: &str = "/tmp/quark";

static VERBOSE: OnceLock<bool> = OnceLock::new();

pub fn is_verbose() -> bool {
    *VERBOSE.get_or_init(|| {
        // Environment variable (Steam launch options: QUARK_VERBOSE=1 %command%)
        if std::env::var("QUARK_VERBOSE").as_deref() == Ok("1") {
            return true;
        }
        // Flag file written by `./install.py --verbose`; it lives outside the
        // compat dir so the installer's clean step does not remove it.
        if let Ok(home) = std::env::var("HOME") {
            let flag = std::path::Path::new(&home)
                .join(".cache")
                .join("quark")
                .join("trace_verbose");
            if flag.exists() {
                return true;
            }
        }
        false
    })
}

pub fn timestamp() -> [u8; 10] {
    let mut tv: libc::timeval = unsafe { std::mem::zeroed() };
    unsafe { libc::gettimeofday(&mut tv, std::ptr::null_mut()) };
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe { libc::localtime_r(&tv.tv_sec, &mut tm) };
    let h = tm.tm_hour as u8;
    let m = tm.tm_min as u8;
    let s = tm.tm_sec as u8;
    let mut buf = *b"[00:00:00]";
    buf[1] = b'0' + h / 10;
    buf[2] = b'0' + h % 10;
    buf[4] = b'0' + m / 10;
    buf[5] = b'0' + m % 10;
    buf[7] = b'0' + s / 10;
    buf[8] = b'0' + s % 10;
    buf
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => {{
        let ts = $crate::log::timestamp();
        let ts = unsafe { std::str::from_utf8_unchecked(&ts) };
        eprintln!("{ts} [INFO]   {}", format_args!($($arg)*));
    }};
}

#[macro_export]
macro_rules! log_warn {
    ($($arg:tt)*) => {{
        let ts = $crate::log::timestamp();
        let ts = unsafe { std::str::from_utf8_unchecked(&ts) };
        eprintln!("{ts} [WARN]   {}", format_args!($($arg)*));
    }};
}

#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => {{
        let ts = $crate::log::timestamp();
        let ts = unsafe { std::str::from_utf8_unchecked(&ts) };
        eprintln!("{ts} [ERROR]  {}", format_args!($($arg)*));
    }};
}

#[macro_export]
macro_rules! log_verbose {
    ($($arg:tt)*) => {{
        if $crate::log::is_verbose() {
            let ts = $crate::log::timestamp();
            let ts = unsafe { std::str::from_utf8_unchecked(&ts) };
            eprintln!("{ts} [DEBUG]  {}", format_args!($($arg)*));
        }
    }};
}
