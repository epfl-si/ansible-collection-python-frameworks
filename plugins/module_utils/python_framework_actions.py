#!/usr/bin/python
# -*- coding: utf-8 -*-

"""OO embroidery to sling Python snippets and AnsiballZ assets directly into your framework.

Support is provided for a variety of approaches to run the framework
code; whether in-process; in a “normal” forked process that shares the
same filesystem; or in a container or snap.
"""

from functools import cached_property
import ast
import errno
import os
import shutil
import subprocess
import sys

from ansible.module_utils.basic import AnsibleModule


class ParserBase:
    """Abstract base class for framework scripts.

    As the sole constructor argument, a subclass receive a **user
    script**, i.e. a snippet of Python (as a string) that typically
    comes directly from the action's YAML code in a play. The job of
    said subclass is to make sense of the script in a way that allows
    to run it.

    Responsibilities of subclass **do not** include spawning a
    subprocess or straight `import`ing and calling frameworks. That is
    the job of one of the `*Runner` classes in the same module.

    Concrete subclasses should parse `self.python_code_string` in the
    constructor, so as both to validate that user script complies with
    whatever format is expected by the parser; and work out the values
    returned by `python_fragment_*` and `python_expression_*` methods.
    """
    def __init__ (self, python_code_string):
        self.python_code_string = python_code_string

    @cached_property
    def ast (self):
        return ast.parse(self.python_code_string)

    def python_fragment_imports (self):
        """Returns the `import foo` lines required by the user script."""
        raise NotImplementedError

    def python_fragment_declarations (self):
        """Returns the classes, functions, etc. that need declaring before the user script can run."""
        raise NotImplementedError

    def python_fragment_initialize (self):
        """Returns any imperative initialization code that must run before the user script can run."""
        return ""

    def python_expression_run_and_return_ansible_result (self):
        """Returns a Python expression that executes the user script as an Ansible task, and returns an Ansible dict containing the execution outcome."""
        raise NotImplementedError


class PostconditionParser (ParserBase):
    """A parser that expects the user script to be implemented as a PostconditionBase subclass.

The script should go something like this:

      from ansible_collections.epfl_si.actions.plugins.module_utils.postconditions import Postcondition as PostconditionBase

      class Postcondition (PostconditionBase):
        def holds (self):
          ...

Yes, this means that the `from ... import ...` line is a piece of
boilerplate that will need copying/pasting into every single task. The
opinion behind this decision is that it makes the user script easier
to read (without requiring any deep understanding of the mechanisms of
`epfl_si.python_frameworks`). This is easy enough to override in a
subclass.
    """
    def __init__ (self, python_postcondition_class_declaration_string):
        super().__init__(python_postcondition_class_declaration_string)
        if self.class_declaration is None:
            raise ValueError("Cannot find Postcondition class")

    @property
    def class_declaration (self):
        """Finds the `PostconditionBase` subclass that the user script defines."""
        for node in ast.walk(self.ast):
            if isinstance(node, ast.ClassDef):
                for superclass in node.bases:
                    if "PostconditionBase" in superclass.id:
                        return node

    def python_fragment_imports (self):
        """Returns the `import` stanza(s) required by the other Python fragments.

        Note that this class does *not* provide PostconditionBase as
        an “ambient” import, despite requiring the user script to make
        a subclass of it. Doing so would arguably make the user script
        harder to read independently

as this would
        arguably be bad style (i.e. it ). Feel free to disagree and
        override this method in a subclass.

        """
        return """
from ansible_collections.epfl_si.actions.plugins.module_utils import postconditions

"""

    def python_fragment_declarations (self):
        return self.python_code_string

    def python_expression_run_and_return_ansible_result (self, check_mode):
        return "postconditions.run_postcondition(%s(), check_mode=%s)" % (
            self.class_declaration.name,
            "True" if check_mode else "False")


class TmpFilesystemBase:
    """Abstract base class for a bunch of temporary files that Ansible will be needing.

    Under this same abstract base class, one can have implementations
    targeted for “trivial“ file systems, in which `copy_file` does
    nothing because the target Python process will share the same file
    namespace; as well as complex cases (i.e. containers) in which
    path translation is required.
    """
    def __init__ (self):
        self.files_to_cleanup = []

    def copy_file (self, from_path):
        """Create (or borrow) `from_path` into the target file system.

        Returns: The path that the Python process should use to refer
        to that file.
        """
        raise NotImplementedError

    def make_file (self, stem, bytes):
        """Create a new, temporary file.

        Returns: The path from inside the container (if any) that the
        Python process should use to refer to the file.
        """
        raise NotImplementedError

    def note_file_created (self, path):
        """Remember one file for (the base class implementation of) `cleanup()`."""
        self.files_to_cleanup.append(path)

    def cleanup (self):
        """Delete any and all files created with `copy_file` and `make_file`.

        The base class calls `self.cleanup_one_file()` on all files
        remembered by `note_file_created()`. Override in a subclass if
        you want a “stateless” way to delete all files, e.g. because
        they are all stored under the same, temporary directory.
        """
        for path in self.files_to_cleanup:
            self.cleanup_one_file(path)

    def cleanup_one_file (self, path):
        """Called by (the base class' implementation of) `cleanup()`."""
        raise NotImplementedError

    @classmethod
    def filenames_like (cls, stem):
        """Yields an infinite series of file names, all different, that ressemble `stem`."""
        yield stem
        (root, suffix) = os.path.splitext(stem)
        counter = 1
        while True:
             yield '%s_%d%s' % (root, counter, suffix)
             counter = counter + 1


class BasicTmpFilesystem (TmpFilesystemBase):
    """The simplest possible implementation of TmpFilesystemBase.

    `copy_file() does nothing; `make_file` creates and returns files
    with unique names under the `tmpdir` constructor argument,
    defaulting to the current directory.
    """

    def __init__ (self, tmpdir=None):
        super().__init__()
        self.tmpdir = tmpdir if tmpdir is not None else os.path.curdir

    def copy_file (self, from_path):
        """Do nothing; just return `from_path`.

        That is, assume that no copying is necessary (i.e. because the
        forked Python process will have access to `from_path` under
        the same path).
        """
        return from_path

    def make_file (self, stem, bytes):
        """Create a file named after `stem`; remember to clean it up."""

        stem = os.path.join(self.tmpdir, stem)
        for path in self.filenames_like(stem):
            with let_EEXIST_slide:
                with open(path, "xb") as fd:
                    fd.write(bytes)
                self.note_file_created(path)
                return path

    def cleanup_one_file (self, path):
        os.unlink(path)


class _SwallowEEXISTContextManager:
    def __enter__ (self):
        return self

    def __exit__ (self, exn_type, exn_value, exn_tb):
        if not exn_type:
            return True
        else:
            return isinstance(exn_value, OSError) and exn_value.errno == errno.EEXIST

let_EEXIST_slide = _SwallowEEXISTContextManager()


class TmpdirFilesystem (TmpFilesystemBase):
    """A filesystem where all temporary files are in the same directory."""
    def __init__ (self, tmpdir_base):
        self.tmpdir_base = tmpdir_base
        self.__has_tmpdir = False

    @cached_property
    def tmpdir (self):
        for path in self.filenames_like(os.path.join(self.tmpdir_base, ".ansible_tmp")):
            with let_EEXIST_slide:
                os.mkdir(path)
                self.__has_tmpdir = True
                return path

    def cleanup (self):
        if self.__has_tmpdir:
            shutil.rmtree(self.tmpdir)

    def copy_file (self, from_path):
        with open(from_path, "rb") as fd:
            slurped_bytes = fd.read()
        return self.make_file(os.path.basename(from_path), slurped_bytes)

    def make_file (self, stem, bytes):
        for path in self.filenames_like(os.path.join(self.tmpdir, stem)):
            with let_EEXIST_slide:
                with open(path, "xb") as fd:
                    fd.write(bytes)
                    return path


class MountedVolumeTmpFilesystem (TmpFilesystemBase):
    """A temporary filesystem that is visible as a mounted subdirectory of the currently accessible filesystem."""
    def __init__ (self, mountpoint, tmpdir_rel_path):
        while mountpoint.endswith("/"):
            mountpoint = mountpoint[:-1]
        self.mountpoint = mountpoint
        self.tmp = TmpdirFilesystem(os.path.join(mountpoint, tmpdir_rel_path))

    def translate_path_outside (self, path_inside):
         # No os.path.join, as path might be absolute:
        return '%s/%s' % (self.mountpoint, path)

    def translate_path_inside (self, path_outside):
        if not path_outside.startswith(self.mountpoint):
            return ValueError

        return path_outside[len(self.mountpoint):]

    def copy_file (self, path_outside):
        return self.translate_path_inside(self.tmp.copy_file(path_outside))

    def make_file (self, stem, bytes):
        return self.translate_path_inside(self.tmp.make_file(stem, bytes))

    def cleanup (self):
        self.tmp.cleanup()


class ForkedRunnerBase:
    """Base class for a runner that runs the user script in a forked Python interpreter.

    This class understands the AnsiballZ API; in particular, it is
    responsible for ensuring that code shipped within the AnsiballZ's
    zip file (i.e. modules with `module_utils` as a path component)
    are available to `import` in the subprocess. This class also knows
    how to report the status according to the AnsiballZ API, i.e. by
    printing a chunk of JSON to standard output.

    Subclasses should define the `python_subprocess_args` method,
    which is where the framework-specific incantations should come in
    (e.g. importing and calling `execute_from_command_line()` for
    Django).

    The `fs` attribute is initialized to an instance of
    `BasicTmpFilesystem()`. In order to e.g. use a Python interpreter
    running in a container or snap, set it to a different
    `TmpFilesystemBase` instance immediately after construction.
    """
    def __init__ (self, script, check_mode):
        self.script = script
        self.check_mode = check_mode
        self.fs = BasicTmpFilesystem()
        self.inhibit_cleanup = False

    def ansiballz_payload_zip_paths (self):
        return [p for p in sys.path
                if "ansible" in p and p.endswith(".zip")]

    @cached_property
    def copied_ansiballz_payloads (self):
        return [self.fs.copy_file(p) for p in self.ansiballz_payload_zip_paths()]
    def python_fragment_set_ansiballz_sys_path (self):
        fragment = "import sys\n"
        for p in self.copied_ansiballz_payloads:
            fragment = fragment + "sys.path.insert(0, '''%s''')\n" % p
        return fragment

    def python_script_multiline_string (self):
        """Returns the full-fledged Python text to be run in the subprocess."""
        return """
import json
import traceback

%(ansiballz)s

%(imports)s

%(declarations)s

%(initialize)s

try:
  result = %(run)s
except Exception as e:
  tb = traceback.format_exc()
  result = dict(failed=True, msg=str(e), traceback=tb)

print(json.dumps(result))

""" % dict(
        ansiballz=self.python_fragment_set_ansiballz_sys_path(),
        imports=self.script.python_fragment_imports(),
        declarations=self.script.python_fragment_declarations(),
        initialize=self.script.python_fragment_initialize(),
        run=self.script.python_expression_run_and_return_ansible_result(self.check_mode))

    def python_subprocess_args (self):
        """Returns the command line (argv) for the Python subprocess."""
        raise NotImplementedError

    def run_and_exit (self):
        """Run the user script and exit in an Ansible-compatible way.

        This means printing the Ansible outcome structure to standard
        output as JSON, and exiting.
        """
        p = subprocess.run(
            args=self.python_subprocess_args(),
            check=False)
        if not self.inhibit_cleanup:
            self.fs.cleanup()
        sys.exit(p.returncode)


class PythonFrameworkActionBase:
    """A nearly-ready-to-use class for your `__main__` to call.

    Just set `runner_class = ` to a class, or equivalently,
    override the `build_runner` method in a subclass of yours.
    """
    module_args = dict(
        # TODO: support imperative (Postcondition-less) scripts here
        postcondition_class=dict(type='str'))

    @cached_property
    def module (self):
        return AnsibleModule(
            argument_spec=self.module_args,
            supports_check_mode=True)

    @cached_property
    def framework_script (self):
        # TODO: support imperative (Postcondition-less) scripts here too
        return PostconditionParser(
            self.module.params['postcondition_class'])

    def build_runner (self, script, check_mode):
        return self.runner_class(script, check_mode)

    def run (self):
        runner = self.build_runner(self.framework_script,
                                   check_mode=self.module.check_mode)

        if hasattr(runner, "set_tmpdir"):
            runner.set_tmpdir(self.module.tmpdir)

        if self.module._keep_remote_files:
            runner.inhibit_cleanup = True

        runner.run_and_exit()
