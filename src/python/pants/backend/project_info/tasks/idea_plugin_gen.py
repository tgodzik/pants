# Copyright 2016 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import json
import logging
import os
import pkgutil
import re
import shutil
import subprocess

from pants.backend.jvm.targets.jvm_target import JvmTarget
from pants.backend.python.targets.python_target import PythonTarget
from pants.base.build_environment import get_buildroot, get_scm
from pants.base.exceptions import TaskError
from pants.base.generator import Generator, TemplateData
from pants.task.console_task import ConsoleTask
from pants.util import desktop
from pants.util.contextutil import temporary_dir, temporary_file
from pants.util.dirutil import safe_mkdir


_TEMPLATE_BASEDIR = 'templates/idea'

# Follow `export.py` for versioning strategy.
IDEA_PLUGIN_VERSION = '0.0.3'


class IdeaPluginGen(ConsoleTask):
  """Invoke IntelliJ Pants plugin (installation required) to create a project.

  The ideal workflow is to programmatically open idea -> select import -> import as pants project -> select project
  path, but IDEA does not have CLI support for "select import" and "import as pants project" once it is opened.

  Therefore, this task takes another approach to embed the target specs into a `iws` workspace file along
  with an skeleton `ipr` project file.

  Sample `iws`:
  ********************************************************
    <?xml version="1.0"?>
    <project version="4">
      <component name="PropertiesComponent">
        <property name="targets" value="[&quot;/Users/me/workspace/pants/testprojects/tests/scala/org/pantsbuild/testproject/cp-directories/::&quot;]" />
        <property name="project_path" value="/Users/me/workspace/pants/testprojects/tests/scala/org/pantsbuild/testproject/cp-directories/" />
      </component>
    </project>
  ********************************************************

  Once pants plugin sees `targets` and `project_path`, it will simulate the import process on and populate the
  existing skeleton project into a Pants project as if user is importing these targets.
  """

  PROJECT_NAME_LIMIT = 200

  @classmethod
  def register_options(cls, register):
    super().register_options(register)
    # TODO: https://github.com/pantsbuild/pants/issues/3198
    # scala/java-language level should use what Pants already knows.
    register('--open', type=bool, default=True,
             help='Attempts to open the generated project in IDEA.')
    register('--incremental-import', type=int, default=None,
             help='Enable incremental import of targets with the given graph depth. Supported '
                  'by IntelliJ Pants plugin versions `>= 1.9.2`.')
    register('--java-encoding', default='UTF-8',
             help='Sets the file encoding for java files in this project.')
    register('--open-with', type=str, default=None, recursive=True,
             help='Program used to open the generated IntelliJ project.')
    register('--debug_port', type=int, default=5005,
             help='Port to use for launching tasks under the debugger.')
    register('--java-jdk-name', default=None,
             help='Sets the jdk used to compile the project\'s java sources. If unset the default '
                  'jdk name for the --java-language-level is used')
    register('--java-language-level', type=int, default=8,
             help='Sets the java language and jdk used to compile the project\'s java sources.')

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

    self.open = self.get_options().open

    self.java_encoding = self.get_options().java_encoding
    self.project_template = os.path.join(_TEMPLATE_BASEDIR,
                                         'project-12.mustache')
    self.workspace_template = os.path.join(_TEMPLATE_BASEDIR,
                                           'workspace-12.mustache')
    self.rootmodule_template = os.path.join(_TEMPLATE_BASEDIR,
                                           'rootmodule-12.mustache')
    self.java_language_level = self.get_options().java_language_level

    if self.get_options().java_jdk_name:
      self.java_jdk = self.get_options().java_jdk_name
    else:
      self.java_jdk = '1.{}'.format(self.java_language_level)

    output_dir = os.path.join(get_buildroot(), ".idea", self.__class__.__name__)
    safe_mkdir(output_dir)

    with temporary_dir(root_dir=output_dir, cleanup=False) as output_project_dir:
      project_name = self.get_project_name(self.context.options.positional_args)

      self.gen_project_workdir = output_project_dir
      self.project_filename = os.path.join(self.gen_project_workdir,
                                           '{}.ipr'.format(project_name))
      self.workspace_filename = os.path.join(self.gen_project_workdir,
                                             '{}.iws'.format(project_name))
      self.rootmodule_filename = os.path.join(self.gen_project_workdir,
                                              'rootmodule.iml')
      self.intellij_output_dir = os.path.join(self.gen_project_workdir, 'out')

  @classmethod
  def get_project_name(cls, target_specs):
    escaped_name = re.sub('[^0-9a-zA-Z:_]+', '.', '__'.join(target_specs))
    # take up to PROJECT_NAME_LIMIT chars as project file name due to filesystem constraint.
    return escaped_name[:cls.PROJECT_NAME_LIMIT]

  # TODO: https://github.com/pantsbuild/pants/issues/3198
  def generate_project(self):
    outdir = os.path.abspath(self.intellij_output_dir)
    if not os.path.exists(outdir):
      os.makedirs(outdir)

    scm = get_scm()
    configured_project = TemplateData(
      root_dir=get_buildroot(),
      outdir=outdir,
      git_root=scm.worktree if scm else None,
      java=TemplateData(
        encoding=self.java_encoding,
        jdk=self.java_jdk,
        language_level='JDK_1_{}'.format(self.java_language_level)
      ),
      debug_port=self.get_options().debug_port,
    )

    abs_target_specs = [os.path.join(get_buildroot(), spec) for spec in self.context.options.positional_args]
    configured_workspace = TemplateData(
      targets=json.dumps(abs_target_specs),
      project_path=os.path.join(get_buildroot(), abs_target_specs[0].split(':')[0]),
      idea_plugin_version=IDEA_PLUGIN_VERSION,
      incremental_import=self.get_options().incremental_import,
    )

    # Generate (without merging in any extra components).
    safe_mkdir(os.path.abspath(self.intellij_output_dir))

    def gen_file(template_file_name, **mustache_kwargs):
      return self._generate_to_tempfile(
        Generator(pkgutil.get_data(__name__, template_file_name).decode(), **mustache_kwargs)
      )

    ipr = gen_file(self.project_template, project=configured_project)
    iws = gen_file(self.workspace_template, workspace=configured_workspace)
    iml_root = gen_file(self.workspace_template)

    shutil.move(ipr, self.project_filename)
    shutil.move(iws, self.workspace_filename)
    shutil.move(iml_root, self.rootmodule_filename)

    return self.project_filename

  def _generate_to_tempfile(self, generator):
    """Applies the specified generator to a temp file and returns the path to that file.
    We generate into a temp file so that we don't lose any manual customizations on error."""
    with temporary_file(cleanup=False, binary_mode=False) as output:
      generator.write(output)
      return output.name

  def console_output(self, _targets):
    if not self.context.options.positional_args:
      raise TaskError("No targets specified.")

    # Heuristics to guess whether user tries to load a python project,
    # in which case intellij project sdk has to be set up manually.
    jvm_target_num = len([x for x in self.context.target_roots if isinstance(x, JvmTarget)])
    python_target_num = len([x for x in self.context.target_roots if isinstance(x, PythonTarget)])
    if python_target_num > jvm_target_num:
      logging.warn('This is likely a python project. Please make sure to '
                   'select the proper python interpreter as Project SDK in IntelliJ.')

    ide_file = self.generate_project()
    yield self.gen_project_workdir

    if ide_file and self.get_options().open:
      open_with = self.get_options().open_with
      if open_with:
        null = open(os.devnull, 'wb')
        subprocess.Popen([open_with, ide_file], stdout=null, stderr=null)
      else:
        try:
          desktop.ui_open(ide_file)
        except desktop.OpenError as e:
          raise TaskError(e)
