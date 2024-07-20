""" This file contains the low level actions that are provided by the environment, mostly file system operations and code execution. """

import os
from pathlib import Path
import subprocess
import selectors
import shutil
import glob
import sys
import inspect
from functools import wraps
import time
from io import StringIO
from .schema import Step, ActionInfo, Action, EnvException
import readline # This is needed to make sure that the input() function works properly


def normalize_args_kwargs(f, *args, **kwargs):
    """ This function takes a function and its arguments and returns a dictionary of the arguments, with the keys being the argument names."""
    sig = inspect.signature(f)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()  # This line is optional, it fills in any omitted arguments that have default values
    return bound.arguments

def append_to_low_level_steps(trace, name, args, observation):
    """ This function appends a low level step to the trace. """
    trace.low_level_steps.append(Step(action=Action(name, args),observation=observation,timestamp=time.time()))


def record_low_level_step(func):
    """ This decorator records a low level step in the trace."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        new_kwargs = normalize_args_kwargs(func, *args, **kwargs)
        if "trace" not in new_kwargs["kwargs"]:
            print("Warning: trace not found in kwargs; not recording low level step.")
            print(func)
            return func(*args, **kwargs)
        else:
            trace = new_kwargs["kwargs"]["trace"]
            for a in LOW_LEVEL_ACTIONS:
                if a.function.__name__ == func.__name__:
                    name = a.name
                    input_args = a.usage.keys()
                    break
            new_kwargs = {k: v for k, v in new_kwargs.items() if k in input_args}
            try:
                observation = func(*args, **kwargs)
                append_to_low_level_steps(trace, name, new_kwargs, observation)
                return observation
            except EnvironmentError as e:
                append_to_low_level_steps(trace, name, new_kwargs, e)
                raise EnvException(e)
    return wrapper


def check_file_read_only(arg_names, **kwargs):
    """ This decorator checks if the file is read-only. """
    
    def inner(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            new_kwargs = normalize_args_kwargs(func, *args, **kwargs)
            for arg_name in arg_names:
                if new_kwargs[arg_name] in new_kwargs["kwargs"]["read_only_files"]:
                    raise EnvException(f"cannot write file {new_kwargs[arg_name]} because it is a read-only file.")
            return func(*args, **kwargs)
        return wrapper
    return inner


def check_file_in_work_dir(arg_names, **kwargs):
    """ This decorator checks if the file is in the work directory. """
    print("running check_file_in_work_dir")
    def inner(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            new_kwargs = normalize_args_kwargs(func, *args, **kwargs)
            work_dir = new_kwargs["work_dir"]
            for arg_name in arg_names:
                file_name = new_kwargs[arg_name]
                if not os.path.abspath(os.path.join(work_dir, file_name)).startswith(os.path.abspath(work_dir)):
                    raise EnvException(f"cannot access file {file_name} because it is not in the work directory.")
            return func(*args, **kwargs)
        return wrapper
    return inner


@check_file_in_work_dir(["dir_path"])
@record_low_level_step
def list_files( dir_path, work_dir = ".", **kwargs):
    max_chars = 10000
    try:
        observation = subprocess.check_output(["ls", os.path.join(work_dir,dir_path)]).decode("utf-8")
        if len(observation) > max_chars:
            observation = observation[:max_chars] + "...[TRUNCATED]"
        return observation
    except Exception as e:
        raise EnvException(f"Cannot list file in the {dir_path} directory: {str(e)}")

    



@check_file_in_work_dir(["file_name"])
@record_low_level_step
def read_file(file_name, work_dir = '.', **kwargs):
    try:
        observation = open(os.path.join(work_dir,file_name)).read()
        return observation
    except Exception as e:
        raise EnvException(f"cannot read file {file_name}: {str(e)}")


@check_file_in_work_dir(["file_name"])
@check_file_read_only(["file_name"])
@record_low_level_step
def write_file(file_name, content, work_dir = ".", **kwargs):
    try:
        with open(os.path.join(work_dir,file_name), "w") as f:
            f.write(content)
        observation = f"File {file_name} written successfully."
        return observation
    except Exception as e:
        raise EnvException(f"cannot write file {file_name}: {str(e)}")


@check_file_in_work_dir(["file_name"])
@check_file_read_only(["file_name"])
@record_low_level_step
def append_file(file_name, content, work_dir = ".", **kwargs):
    try:
        with open(os.path.join(work_dir,file_name), "a") as f:
            f.write(content)
        observation = f"File {file_name} appended successfully."
        return observation
    except Exception as e:
        raise EnvException(f"cannot append file {file_name}: {str(e)}")


@check_file_in_work_dir(["source", "destination"])
@check_file_read_only(["destination"])
@record_low_level_step
def copy_file( source, destination, work_dir = ".", **kwargs):
    
    try:
        shutil.copyfile(os.path.join(work_dir,source), os.path.join(work_dir,destination))
        observation = f"File {source} copied to {destination}"
        return observation
    except Exception as e:
        raise EnvException(f"File {source} copy to {destination} failed. Check whether the source and destinations are valid: {str(e)}")


@check_file_in_work_dir(["script_name"])
@record_low_level_step
def undo_edit_script( script_name, work_dir = ".", **kwargs):
    
    backup_files = glob.glob(os.path.join(work_dir,"backup", f"{script_name}_*"))
    if len(backup_files) == 0:
        raise EnvException("There is no change to undo.")
    try:
        backup_files.sort()
        backup_file = backup_files[-1]
        shutil.copyfile(backup_file, os.path.join(work_dir,script_name))
        # delete the backup file
        os.remove(backup_file)

        new_content = open(os.path.join(work_dir,script_name)).read()
        observation = f"Content of {script_name} after undo the most recent edit:\n" + new_content
        return observation
    except Exception as e:
        raise EnvException(f"Cannot undo the edit of file name {script_name}. Check the file name again: {str(e)}")


@check_file_in_work_dir(["script_name"])
@record_low_level_step
def execute_script(script_name, work_dir = ".", **kwargs):
    if not os.path.exists(os.path.join(work_dir,script_name)):
        raise EnvException(f"The file {script_name} does not exist.")
    try:
        script_path = script_name
        device = kwargs["device"]
        python = kwargs["python"]
        cmd = f"CUDA_VISIBLE_DEVICES={device} {python} -u {script_path}"
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True, cwd=work_dir)

        stdout_lines = []
        stderr_lines = []

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)

        while process.poll() is None and selector.get_map():
            events = selector.select(timeout=1)

            for key, _ in events:
                line = key.fileobj.readline()
                if key.fileobj == process.stdout:
                    print("STDOUT:", line, end =" ")
                    stdout_lines.append(line)
                else:
                    print("STDERR:", line, end =" ")
                    stderr_lines.append(line)

        for line in process.stdout:
            line = line
            print("STDOUT:", line, end =" ")
            stdout_lines.append(line)
        for line in process.stderr:
            line = line
            print("STDERR:", line, end =" ")
            stderr_lines.append(line)

        return_code = process.returncode

        if return_code != 0:
            observation = "".join(stderr_lines)
        else:
            observation = "".join(stdout_lines)
        if observation == "" and return_code == 0:
            # printed to stderr only
            observation = "".join(stderr_lines)
        return "The script has been executed. Here is the output:\n" + observation
    except Exception as e:
        raise EnvException(f"Something went wrong in executing {script_name}: {e}. Please check if it is ready to be executed.")


@record_low_level_step
def python_repl(command, work_dir = ".", **kwargs):
    """Run command and returns anything printed."""
    try:
        cwd = os.getcwd()
        import codeop
        compiler = codeop.CommandCompiler()
        old_stdout = sys.stdout
        sys.stdout = mystdout = StringIO()
        try:
            command = compiler(command)
            os.chdir(work_dir)
            exec(command, globals())
            sys.stdout = old_stdout
            output = mystdout.getvalue()
        except Exception as e:
            sys.stdout = old_stdout
            output = str(e)
        os.chdir(cwd)
        return output
    except Exception as e:
        raise EnvException(f"Something went wrong in executing {command}: {e}")


@record_low_level_step
def request_help(request, work_dir = ".", **kwargs):
    return input(f"Research Assistant is requesting help: {request}\n")


# @check_file_in_work_dir(["dir_path"])
# @record_low_level_step
# def list_files( dir_path, work_dir = ".", **kwargs):
#     max_chars = 10000
#     try:
#         observation = subprocess.check_output(["ls", os.path.join(work_dir,dir_path)]).decode("utf-8")
#         if len(observation) > max_chars:
#             observation = observation[:max_chars] + "...[TRUNCATED]"
#         return observation
#     except Exception as e:
#         raise EnvException(f"Cannot list file in the {dir_path} directory: {str(e)}")

@check_file_in_work_dir(["submission_path"])
@record_low_level_step
def validate_submission(submission_path, work_dir = ".", **kwargs):
    print("running validate_submission")
    print("kwargs", kwargs)
    submission_path = kwargs["submission_path"]

    print("submission_path", submission_path)
    submission_path = Path(submission_path)
    print("submission_path.exists()", submission_path.exists())
    if not submission_path.exists():
        return f"Submission file {submission_path} does not exist."
    submission_path = submission_path.resolve() # Make the path absolute
    print("submission_path", submission_path)

    try:
        print("trying")
        observation = subprocess.check_output(["bash", "/home/validate_submission.sh", submission_path], cwd=work_dir).decode("utf-8")
        print("observation", observation)
        return observation
    except Exception as e:
        print("excepted", e)
        raise EnvException(f"Failed to validate submission file {submission_path}: {str(e)}")


### describe the low level actions
LOW_LEVEL_ACTIONS = [
    ActionInfo(
        name="List Files",
        description="Use this to navigate the file system.",
        usage={   
            "dir_path": "a valid relative path to a directory, such as \".\" or \"folder1/folder2\""
        },
        return_value="The observation will be a list of files and folders in dir_path or current directory is dir_path is empty, or an error message if dir_path is invalid.",
        function=list_files,
        is_primitive=True
    ),
    ActionInfo(
        name="Read File",
        description="Use this to read an existing file.",
        usage={
            "file_name": "a valid file name with relative path to current directory if needed"
        },
        return_value="The observation will be the contents of the file read.",
        function=read_file,
        is_primitive=True
    ),
    ActionInfo(
        name="Write File",
        description="Use this to write a file. If the file already exists, it will be overwritten.",
        usage={
            "file_name": "a valid file name with relative path to current directory if needed",
            "content": "the content to be written to the file"
        },
        return_value="A success message if the file is written successfully, or an error message if the file cannot be written.",
        function=write_file,
        is_primitive=True
    ),
    ActionInfo(
        name="Append File",
        description="Use this to append a file to a new location with a new name.",
        usage={
            "file_name": "a valid file name with relative path to current directory if needed",
            "content": "the content to be appended to the file"
        },
        return_value="A success message if the file is appended successfully, or an error message if the file cannot be appended.",
        function=append_file,
        is_primitive=True
    ),
    ActionInfo(
        name="Copy File",
        description="Use this to copy a file to a new location with a new name.",
        usage={
            "source": "a valid file name with relative path to current directory if needed",
            "destination": "a valid file name with relative path to current directory if needed"
        },
        return_value="A success message if the file is copied successfully, or an error message if the file cannot be copied.",
        function=copy_file,
        is_primitive=True
    ),
    ActionInfo(
        name="Undo Edit Script",
        description="Use this to undo the last edit of the python script.",
        usage={
            "script_name": "a valid python script name with relative path to current directory if needed"
        },
        return_value="The observation will be the content of the script before the last edit. If the script does not exist, the observation will be an error message.",
        function=undo_edit_script,
        is_primitive=True
    ),
    ActionInfo(
        name="Execute Script",
        description="Use this to execute the python script. The script must already exist.",
        usage={
            "script_name": "a valid python script name with relative path to current directory if needed"
        },
        return_value="The observation will be output of the script or errors.",
        function=execute_script,
        is_primitive=True
    ),
    ActionInfo(
        name="Python REPL",
        description="A python REPL. Use this to execute single line python commands.",
        usage={
            "command": "a valid python command"
        },
        return_value="The observation will be output of the command or errors.",
        function=python_repl,
        is_primitive=True 
    ),
    ActionInfo(
        name="Request Help",
        description="Use this to request help from human. Use this only when the provided tools and files are not enough for accomplishing necessary steps, such as requesting API reference or installing a library. So you should check through the provided tools and files first.",
        usage={
            "request": "a detailed description on what to do"
        },
        return_value="The observation will be the response from human.",
        function=request_help,
        is_primitive=True
    ),
    ActionInfo(
        name="Validate Submission",
        description="Use the benchmark-provided tool to validate the format of your submission. You must provide the path to a submission file.",
        usage={
            "submission_path": "path to the file you wish to validate, e.g. submission/submission.csv (path should be relative to your current working directory)",
        },
        return_value="The observation is a message informing you whether your submission file is in a format compatible for grading or not.",
        function=validate_submission,
        is_primitive=True,
    ),
    ActionInfo(
        name="Final Answer",
        description="Use this to indicate completion. IMPORTANT: You will not be able to perform any other actions after this one. Use this only when you are sure that you have completed the task (e.g. you have provided your submission as instructed).",
        usage={
            "final_answer": "summary of what you have accomplished and the final state of the task"
        },
        return_value="The observation will be empty.",
        function=(lambda **kwargs: ""),
        is_primitive=True
    ),
]