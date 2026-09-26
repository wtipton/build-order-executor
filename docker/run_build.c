// Setuid shim: the ONLY path from the unprivileged `agent` user to the game engine.
//
// It does as little as possible on purpose — this runs as root, so every line here is
// attack surface. All policy (argument validation, the attempt ledger, the run cap)
// lives in _attempt.py, which this execs.
//
// Two hardening rules it exists to enforce:
//
//   1. The environment is REBUILT, not inherited. The kernel strips LD_PRELOAD across a
//      setuid exec but does NOT strip PYTHONPATH, so inheriting the caller's environment
//      into a root python would let the agent inject a module and execute code as root.
//      We execve() with a fixed envp instead of execv().
//   2. python runs with -I (isolated): ignores all PYTHON* variables, skips user
//      site-packages, and does not put the script's directory on sys.path. Belt and
//      braces with (1). _attempt.py therefore imports stdlib only.
//
// Built and installed by docker/Dockerfile; see docker/README.md.

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define PYTHON "/opt/venv/bin/python"
#define ATTEMPT "/opt/executor/_attempt.pyc"

// The complete environment the engine runs with. SC2_TARGET selects the native headless
// launcher in run.py; SC2PATH points at the (root-only) game install.
static char *const ENVP[] = {
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "HOME=/root",
    "SC2PATH=/opt/StarCraftII",
    "SC2_TARGET=linux",
    "LANG=C.UTF-8",
    NULL,
};

int main(int argc, char *argv[]) {
    // Group before user: once euid is dropped to a non-root value setgid() would fail.
    // (We start with euid 0 from the setuid bit, so both succeed here.)
    if (setgid(0) != 0) {
        perror("setgid failed");
        return 1;
    }
    if (setuid(0) != 0) {
        perror("setuid failed");
        return 1;
    }

    // python -I /opt/executor/_attempt.pyc [caller args...]
    char **new_argv = malloc((argc + 3) * sizeof(char *));
    if (!new_argv) {
        perror("malloc failed");
        return 1;
    }

    new_argv[0] = PYTHON;
    new_argv[1] = "-I";
    new_argv[2] = ATTEMPT;
    for (int i = 1; i < argc; i++) {
        new_argv[i + 2] = argv[i];
    }
    new_argv[argc + 2] = NULL;

    execve(PYTHON, new_argv, ENVP);
    perror("execve failed");
    return 1;
}
