import os
import sublime
import sublime_plugin
import re
import subprocess
import tempfile
from subprocess import Popen, PIPE


settings = None


class Settings:
    def __init__(self):
        package_settings = sublime.load_settings("RustAutoComplete.sublime-settings")
        package_settings.add_on_change("racer", settings_changed)
        package_settings.add_on_change("search_paths", settings_changed)

        self.racer_bin = package_settings.get("racer", "racer")
        self.search_paths = package_settings.get("search_paths", [])
        self.package_settings = package_settings

    def unload(self):
        self.package_settings.clear_on_change("racer")
        self.package_settings.clear_on_change("search_paths")


def plugin_loaded():
    global settings
    settings = Settings()


def plugin_unloaded():
    global settings
    if settings != None:
        settings.unload()
        settings = None


def settings_changed():
    global settings
    if settings != None:
        settings.unload()
        settings = None
    settings = Settings()


class Result:
    def __init__(self, parts):
        self.completion = parts[0]
        self.snippet = parts[1]
        self.row = int(parts[2])
        self.column = int(parts[3])
        self.path = parts[4]
        self.type = parts[5]
        self.context = parts[6]


def expand_all(paths):
    return [os.path.expanduser(path)
            for path in paths]

def determine_context_path(view):
    """Returns a path suitable for racer's first input-file argument.
       - Ideally, this is the path of an actual file on disk.
       - If the open file hasn't been saved, then the path should be to a (non-existent)
         hypothetical path near the user's other rust modules; racer will find the Cargo
         root if possible and provide completions from the user's modules.
       - If all else fails, we can provide a dummy name like '-' (we always provide a
         'substitute' file over stdin, so racer doesn't actually try to read it)."""

    # If the current view has a path, then we can use it directly (first case)
    if view.file_name() is not None:
        return view.file_name()

    # Otherwise, we try to assume a path based on other open documents
    paths = [v.file_name() for v in view.window().views() if v.file_name() is not None]
    # We only care about open rust files
    paths = [path for path in paths if path[-3:] == ".rs"]
    directories = [os.path.join(os.path.dirname(path), "_transient.rs") for path in paths]

    # Dummy path (third case)
    if len(directories) == 0:
        return "-"

    # Count the frequency of occurance of each path
    dirs = {}
    for item in directories:
        if item not in dirs:
            dirs[item] = 1
        else:
            dirs[item] += 1

    # Use the most common path
    return max(dirs.keys(), key=(lambda key: dirs[key]))

def run_racer(view, cmd_list):
    # Retrieve the entire buffer
    region = sublime.Region(0, view.size())
    content = view.substr(region)
    with_snippet = cmd_list[0] == "complete-with-snippet"

    cmd_list.insert(0, settings.racer_bin)

    # We always have a 'context path' which is ideally near the user's other rust modules.
    # Racer echos this in output (even if it is a dummy name) to indicate matches within the
    # open document. Note that this does not always point to an existent file.
    context_path = determine_context_path(view)

    # We provide the context path as the (required) primary input (but it isn't read; see next arg).
    cmd_list.append(context_path)
    # The optional last argument to find-definition and complete-with-snippet is the [substitute_file],
    # i.e. the file to use for actual text content. '-' is a magic value that means stdin. So, we have a
    # command line like `racer ??? context_path/ - where context_path/ may or may not exist (we provide its 
    # presumed content over stdin).
    cmd_list.append("-")

    # Copy the system environment and add the source search
    # paths for racer to it
    env = os.environ.copy()
    expanded_search_paths = expand_all(settings.search_paths)
    if 'RUST_SRC_PATH' in env:
        expanded_search_paths.append(env['RUST_SRC_PATH'])
    env['RUST_SRC_PATH'] = os.pathsep.join(expanded_search_paths)

    # Run racer
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    process = Popen(cmd_list, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, startupinfo=startupinfo)
    (output, err) = process.communicate(input=content.encode("utf-8"))
    exit_code = process.wait()

    # Parse results
    results = []
    match_string = "MATCH "
    if exit_code == 0:
        for byte_line in output.splitlines():
            line = byte_line.decode("utf-8")
            if line.startswith(match_string):
                if with_snippet:
                    parts = line[len(match_string):].split(';', 7)
                else:
                    parts = line[len(match_string):].split(',', 6)
                    parts.insert(1, "")

                result = Result(parts)
                results.append(result)
    else:
        print("CMD: '%s' failed: exit_code:" % ' '.join(cmd_list), exit_code, output, err)
    return results


class RustAutocomplete(sublime_plugin.EventListener):
    def on_query_completions(self, view, prefix, locations):
        # Check if this is a Rust source file. This check
        # relies on the Rust syntax formatting extension
        # being installed - https://github.com/jhasse/sublime-rust
        if view.match_selector(locations[0], "source.rust"):
            # Get the buffer location in correct format for racer
            row, col = view.rowcol(locations[0])
            row += 1

            try:
                raw_results = run_racer(view, ["complete-with-snippet", str(row), str(col)])
            except FileNotFoundError:
                print("Unable to find racer executable (check settings)")
                return

            def cmp(raw_result):
                return {
                    "Module": 0,
                    "Function": 1,
                    "Struct": 2,
                    "Trait": 3,
                    "Type": 4,
                    "Enum": 5
                }.get(raw_result.type, 100)

            raw_results = sorted(raw_results, key = cmp)

            lalign = 0;
            ralign = 0;
            for result in raw_results:
                lalign = max(lalign, len(result.completion) + len(result.type))
                ralign = max(ralign, len(result.context))

            results = []
            longest = 0;
            for result in raw_results:
                context = " : {}".format(result.context) if result.type != "Module" else ""
                # TODO: consider using \t -> snippet description style
                completion = "{0}   {1:>{2}}{3}".format(result.completion, result.type, lalign - len(result.completion), context)
                longest = max(longest, len(completion))
                results.append((completion, result.snippet))

            # print(results)
            if len(results) > 0:
                # print(results)
                # add padding at the end of the first entry as that appears to set the popup width
                completion = "{0}{1:>{2}}".format(results[0][0], '', max(0, longest - len(results[0][0]) - 2))
                results[0] = (completion, results[0][1])
                return (results, sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)


class RustGotoDefinitionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # Get the buffer location in correct format for racer
        row, col = self.view.rowcol(self.view.sel()[0].begin())
        row += 1

        results = run_racer(self.view, ["find-definition", str(row), str(col)])

        if len(results) == 1:
            result = results[0]
            path = result.path
            # On Windows the racer will return the paths without the drive
            # letter and we need the letter for the open_file to work.
            if sublime.platform() == 'windows' and not re.compile('^\w\:').match(path):
                path = 'c:' + path
            encoded_path = "{0}:{1}:{2}".format(path, result.row, result.column)
            self.view.window().open_file(encoded_path, sublime.ENCODED_POSITION)
