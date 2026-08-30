#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    // Elevate privileges to root so /opt/executor can be read
    if (setuid(0) != 0 || setgid(0) != 0) {
        perror("setuid/setgid failed");
        return 1;
    }

    // Build argument list: python /opt/executor/_runner.pyc [args...]
    char **new_argv = malloc((argc + 2) * sizeof(char *));
    if (!new_argv) {
        perror("malloc failed");
        return 1;
    }

    new_argv[0] = "/usr/local/bin/python";
    new_argv[1] = "/opt/executor/_runner.pyc";
    for (int i = 1; i < argc; i++) {
        new_argv[i + 1] = argv[i];
    }
    new_argv[argc + 1] = NULL;

    execv("/usr/local/bin/python", new_argv);
    perror("execv failed");
    return 1;
}
