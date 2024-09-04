# Ansible Collection - `epfl_si.python_frameworks`

This collection requires `epfl_si.actions` and provides further assistance for writing one particular kind of Ansible actions; namely, those that transmit some user-authored Python code to run in a framework (such as Django), in order to enforce the desired postconditions.

  > **This is Alpha-quality software.**

Class names may change with no warning. We might decide to fold this whole collection into `epfl_si.actions`. Watch this space.

# Motivation and Example

Suppose you would like to say the following in your Ansible play:

```yaml
- name: "MAAS Ubuntu mirror"
  maas_script:
    postcondition_class: |
      from ansible_collections.epfl_si.actions.plugins.module_utils.postconditions import Postcondition as PostconditionBase
      from maasserver.models.packagerepository import PackageRepository

      class Postcondition (PostconditionBase):
        def __init__ (self):
          self.url = "{{ _ubuntu_main_mirror }}"

        @property
        def main_archive (self):
          return PackageRepository.objects.get(name="main_archive")

        def holds (self):
          return self.main_archive.url == self.url

        def enforce (self):
          main_archive = self.main_archive
          main_archive.url = self.url
          main_archive.save()
  vars:
    _ubuntu_main_mirror: "{{ install_server_maas_ubuntu_main_mirror }}"
  tags: install-server.maas.config
```

In this Ansible task, the `postcondition_class` YAML sub-field is a Python snippet, hereinafter named the **user script**. It consists of a `Postcondition` class, written in terms of the `PostconditionBase` abstract base class provided by the `epfl_si.actions` Ansible collection. Its `holds()` and `enforce()` methods are themselves written in terms of checks resp. mutations running on top of the Django ORM.

The purpose of `epfl_si.python_frameworks` is to help you ship the user script (as well as its Ansible-side dependencies) over ssh and transparently run it in-framework; even if said framework is hidden e.g. inside a container or snap. Here is what your role or playbook's `library/maas_script.py` would look like:

```python
from ansible.module_utils.basic import AnsibleModule    # ①
from ansible_collections.epfl_si.python_frameworks.plugins.module_utils.python_framework_actions import MountedVolumeTmpFilesystem, PythonFrameworkActionBase, ForkedRunnerBase  # ②
from ansible_collections.epfl_si.actions.plugins.module_utils import postconditions  # ③


class SnapMaasRunner (ForkedRunnerBase):  # ④
    def __init__ (self, *args, **kwargs):
        super().__init__( *args, **kwargs)
        self.fs = MountedVolumeTmpFilesystem("/var/snap/maas/current", "root")  # ⑤

    def python_subprocess_args (self): # ⑥
        script_path_inside_snap = self.fs.make_file(    # ⑦
            "maas_script.py",
            self.python_script_multiline_string().encode('utf-8'))  # ⑧
        return [
            'snap', 'run', '--shell',
            'maas', '-c', 'exec python3 "$@"', '--',
            '-c',
            'from maascli import snap_setup; snap_setup();' +
            ' from maasserver import execute_from_command_line;' +
            ' execute_from_command_line()',
            'shell', '-c', 'exec(open("%s").read())' % script_path_inside_snap
            ]


class MaasScriptAction (PythonFrameworkActionBase):  # ⑨
    runner_class = SnapMaasRunner


if __name__ == '__main__':
    MaasScriptAction().run()
```

① As per Ansible convention for files living under `library/`,
`maas_script.py` defines a so-called Ansible *module*; that is, a
program that interacts with Ansible through a CLI, receiving its code
and run-time parameters (which, in this case, also contains more code)
as part of a self-executable Zip file (a so-called “AnsiballZ”) sent
over ssh beforehand; and expecting results as a JSON data structure
printed on the standard output with top-level keys such as `changed`,
`failed`, `message` and more.

② This particular Ansible module is written in terms of a few classes
(some of them abstract) provided by the `epfl_si.python_frameworks`
collection.

- We select `ForkedRunnerBase`, because we are going to run the in-framework code in a separate process; in this particular case we have little choice, because MAAS lives in a snap (a kind of container used to distribute portable and secure Linux software). All the required Django code, as well as the database credentials, are available inside that snap.
- `MountedVolumeTmpFilesystem` is responsible for providing the main Python script and its dependencies (i.e. the AnsiballZ Zip file) to the forked process. Because the latter runs in a snap, there is some file copying and path translation to be done between “outside” the snap (where the AnsiballZ executable Zip runs) and “inside” (where the subprocess runs).  The `MountedVolumeTmpFilesystem` class provides these capabilities to the `*Runner*` instance that has-an instance of it as `runner.fs`.
- The `PythonFrameworkActionBase` is what provides the “main” entry point to your module; it creates and holds (directly or indirectly) references to instances of the other two classes.

③ The astute reader will notice that this import appears unused. However, the AnsiballZ builder logic (the one that runs as part of the `ansible-playbook` in the operator's workstation) has some tree-shaking capabilities. Specifically, it recursively scans Python code for `import`s that contain a `module_utils` component, and makes sure to embed the corresponding Python files in the AnsiballZ zip. Hence this import is a way to make sure that `ansible_collections.epfl_si.actions.plugins.module_utils.postconditions` gets shipped over to the remote side. A future version of `epfl_si.python_frameworks` might make such a workaround obsolete; however, it would require some extra intelligence on the operator workstation's side i.e. we would have to write an Ansible *action plugin* instead of (or more likely, in addition to) an Ansible module.

④ As the `*Base` suffix in the class name implies,  `ForkedRunnerBase` is meant to be inherited from, rather than instantiated as-is. Documentation is provided in each method of each class (abstract or otherwise) for subclassing purposes.

⑤ `SnapMaasRunner` is our **runner**; that is, the class responsible for generating and positioning all files, running the subprocess, and finally cleaning up. Its constructor is overridden so as use something else as `self.fs` than the defuault `BasicTmpFilesystem`, which would not know how to perform path translation from / to the MAAS snap (or indeed do any copying of the Ansiballz at all, as it assumes it can get away with pointing the subprocess to the original file). The `MountedVolumeTmpFilesystem` that we construct and use instead, is set up to add / remove the `/var/snap/maas/current` prefix when transating paths between inside and outside the snap; and it stores all the files it creates in temporary directories under the `root` subdirectory thereof, i.e. with full (outside) paths like `/var/snap/maas/current/root/.ansible` or `/var/snap/maas/current/root/.ansible_1` etc.

⑥ Overriding `python_subprocess_args` to work out the command line (a.k.a. `argv`) of the subprocess, is something that any `ForkedRunnerBase` subclass will need to do, as that method in the base class is abstract (that is, it just raises `NotImplementedError` when called). This makes sense, since `epfl_si.python_frameworks` cannot possibly know how to load the framework you use, nor how to run the Python code. Here, we run a suitable Russian doll assembly of `snap run`, Python, Django initialization, and good ole shell, that ends up running the user script.

⑦ In this particular implementation, we decided to use an intermediate temporary file to write the (massaged, see below) user script into (as opposed to just splatting the whole multi-line script onto the command line after `shell -c`, which would also work just fine). This is mainly because it makes the whole thing easier to debug. `epfl_si.python_frameworks` honors `ANSIBLE_KEEP_REMOTE_FILES`; setting it to 1 in the environment of `ansible-playbook`, will also prevent the temporary files that `epfl_si.python_frameworks` creates from being cleaned up upon termination.

⑧ `self.python_script_multiline_string()` doesn't just spit out the user script as-is; running that would not do much (since as you can see, it just defines a class, and exits). Rather, the runner's `.python_script_multiline_string()` (with help from the action class, coming up next; and more `*Runnable*` classes that are not shown here) automagically weaves the user script into some prologue and epilogue code that is required to actually run the task.

⑨ The `PythonFrameworkActionBase` provides you with the module's main entry point. It innately knows that user scripts passed as a `postcondition_class` need such-and-such Python wrappage (see ⑧, above); and it orchestrates the creation of all the other objects, starting at the `runner_class` that is set on the next line.

# Planned Features

- Support for imperative (not `PostconditionBase`-based) scripts that manipulate the Ansible return structure directly
- In-process use case (required by [wp-ops](https://github.com/epfl-si/wp-ops)' [awx_script](https://github.com/epfl-si/wp-ops/blob/master/ansible/roles/awx-instance/library/awx_script.py))
