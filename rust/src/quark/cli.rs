// Command-line parsing: Steam invokes us with a Proton verb. Arguments stay
// OsString so a game path that is not UTF-8 reaches Wine unchanged.

use crate::log_error;
use std::ffi::OsString;

const PROTON_VERBS: &[&str] = &[
    "waitforexitandrun",
    "run",
    "getcompatpath",
    "getnativepath",
];

pub fn parse_args() -> (String, Vec<OsString>) {
    let args: Vec<OsString> = std::env::args_os().collect();
    if args.len() < 2 {
        print_usage();
        std::process::exit(2);
    }

    let cmd = args[1].to_string_lossy();
    if !PROTON_VERBS.contains(&cmd.as_ref()) {
        log_error!("Unknown verb: {cmd}");
        print_usage();
        std::process::exit(1);
    }
    (cmd.into_owned(), args[2..].to_vec())
}

fn print_usage() {
    eprintln!("Usage: quark <verb> <exe> [args...]");
    eprintln!("  verbs: {}", PROTON_VERBS.join(", "));
}
