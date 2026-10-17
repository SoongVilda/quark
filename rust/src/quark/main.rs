// quark -- Proton replacement launcher
//
// Steam invokes us as: ./proton <verb> <exe> [args...]

mod log;
mod cli;
mod launcher;
mod pe_scanner;

fn main() {
    let (verb, args) = cli::parse_args();
    std::process::exit(launcher::run(&verb, &args));
}
