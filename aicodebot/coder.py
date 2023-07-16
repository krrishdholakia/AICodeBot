from aicodebot.config import read_config
from aicodebot.helpers import exec_and_get_output, logger
from langchain.chat_models import ChatOpenAI
from openai.api_resources import engine
from pathlib import Path
import fnmatch, functools, openai, tiktoken

DEFAULT_MAX_TOKENS = 512
PRECISE_TEMPERATURE = 0.05
CREATIVE_TEMPERATURE = 0.6


class Coder:
    """
    The Coder class encapsulates the functionality of interacting with LLMs,
    git, and the local file system.
    """

    @classmethod
    def generate_directory_structure(cls, path, ignore_patterns=None, use_gitignore=True, indent=0):
        """Generate a text representation of the directory structure of a path."""
        ignore_patterns = ignore_patterns.copy() if ignore_patterns else []

        base_path = Path(path)

        if use_gitignore:
            # Note: .gitignore files can exist in sub directories as well, such as * in __pycache__ directories
            gitignore_file = base_path / ".gitignore"
            if gitignore_file.exists():
                with gitignore_file.open() as f:
                    ignore_patterns.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))

        structure = ""
        if base_path.is_dir():
            if not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
                structure += "  " * indent + f"- [Directory] {base_path.name}\n"
                for item in base_path.iterdir():
                    structure += cls.generate_directory_structure(item, ignore_patterns, use_gitignore, indent + 1)
        elif not any(fnmatch.fnmatch(base_path.name, pattern) for pattern in ignore_patterns):
            structure += "  " * indent + f"- [File] {base_path.name}\n"

        return structure

    @staticmethod
    @functools.lru_cache
    def get_openai_supported_engines():
        """Get a list of the models supported by the OpenAI API key."""
        config = read_config()
        openai.api_key = config["openai_api_key"]
        engines = engine.Engine.list()
        out = [engine.id for engine in engines.data]
        logger.trace(f"OpenAI supported engines: {out}")
        return out

    @staticmethod
    def get_llm(
        model_name,
        verbose=False,
        response_token_size=DEFAULT_MAX_TOKENS,
        temperature=PRECISE_TEMPERATURE,
        live=None,
        streaming=False,
        callbacks=None,
    ):
        config = read_config()
        if "openrouter_api_key" in config:
            # If the openrouter_api_key is set, use the Open Router API
            # OpenRouter allows for access to many models that have larger token limits
            openai.api_key = config["openrouter_api_key"]
            openai.api_base = "https://openrouter.ai/api/v1"
            headers = {"HTTP-Referer": "https://aicodebot.dev", "X-Title": "AICodeBot"}
            tiktoken_model_name = model_name.replace("openai/", "")
        else:
            openai.api_key = config["openai_api_key"]
            headers = None
            tiktoken_model_name = model_name

        return ChatOpenAI(
            openai_api_key=openai.api_key,
            model=model_name,
            max_tokens=response_token_size,
            verbose=verbose,
            temperature=temperature,
            streaming=streaming,
            callbacks=callbacks,
            tiktoken_model_name=tiktoken_model_name,
            model_kwargs={"headers": headers},
        )

    @staticmethod
    def get_llm_headers():
        config = read_config()
        if "openrouter_api_key" in config:
            return {"HTTP-Referer": "https://aicodebot.dev", "X-Title": "AICodeBot"}
        else:
            return None

    @staticmethod
    def get_llm_model_name(token_size=0):
        config = read_config()
        if "openrouter_api_key" in config:
            model_options = {
                "openai/gpt-4": 8192,
                "openai/gpt-4-32k": 32768,
                # Not working yet "anthropic/claude-2": 100_000,
            }
            supported_engines = model_options.keys()
        else:
            model_options = {
                "gpt-4": 8192,
                "gpt-4-32k": 32768,
                "gpt-3.5-turbo": 4096,
                "gpt-3.5-turbo-16k": 16384,
            }
            # Pull the list of supported engines from the OpenAI API for this key
            supported_engines = Coder.get_openai_supported_engines()

        # For some unknown reason, tiktoken often underestimates the token size by ~5%, so let's buffer
        token_size = int(token_size * 1.05)

        for model, max_tokens in model_options.items():
            if model in supported_engines and token_size <= max_tokens:
                logger.info(f"Using {model} for token size {token_size}")
                return model

        logger.critical(f"The context is too large ({token_size}) for any of the models supported by your API key. 😞")
        if "openrouter_api_key" not in config:
            logger.critical("If you provide an Open Router API key, you can access larger models, up to 32k tokens")

        return None

    @staticmethod
    def get_token_length(text, model="gpt-4"):
        """Get the number of tokens in a string using the tiktoken library."""
        encoding = tiktoken.encoding_for_model(model)
        tokens = encoding.encode(text)
        token_length = len(tokens)
        short_text = text.strip()[0:20] + "..." if len(text) > 10 else text
        logger.debug(f"Token length for {short_text}: {token_length}")
        return token_length

    @staticmethod
    def git_diff_context(commit=None, files=None):
        """Get a text representation of the git diff for the current commit or staged files, including new files"""
        base_git_diff = ["git", "diff", "-U10"]  # Tell diff to provide 10 lines of context

        if commit:
            # If a commit is provided, just get the diff for that commit
            logger.debug(f"Getting diff for commit {commit}")
            # format=%B is the diff and the commit message
            show = exec_and_get_output(["git", "show", "--format=%B", commit])
            logger.opt(raw=True).debug(f"Diff for commit {commit}: {show}")
            return show
        else:
            # Otherwise, get the diff for the staged files, or if there are none, the diff for the unstaged files
            staged_files = Coder.git_staged_files()
            if staged_files:
                logger.debug(f"Getting diff for staged files: {staged_files}")
                diff_type = "--cached"
            else:
                diff_type = "HEAD"

            file_status = exec_and_get_output(
                ["git", "diff", diff_type, "--name-status"] + list(files or [])
            ).splitlines()

            diffs = []
            for status in file_status:
                status_parts = status.split("\t")
                status_code = status_parts[0][0]  # Get the first character of the status code
                if status_code == "A":
                    # If the file is new, include the entire file content
                    file_name = status_parts[1]
                    contents = Path(file_name).read_text()
                    diffs.append(f"## New file added: {file_name}")
                    diffs.append(contents)
                elif status_code == "R":
                    # If the file is renamed, get the diff and note the old and new names
                    old_file_name, new_file_name = status_parts[1], status_parts[2]
                    diffs.append(f"## File renamed: {old_file_name} -> {new_file_name}")
                    diffs.append(exec_and_get_output(base_git_diff + [diff_type, "--", new_file_name]))
                elif status_code == "D":
                    # If the file is deleted, note the deletion
                    file_name = status_parts[1]
                    diffs.append(f"## File deleted: {file_name}")
                else:
                    # If the file is not new, renamed, or deleted, get the diff
                    file_name = status_parts[1]
                    diffs.append(f"## File changed: {file_name}")
                    diffs.append(exec_and_get_output(base_git_diff + [diff_type, "--", file_name]))

            return "\n".join(diffs)

    @staticmethod
    def git_staged_files():
        return exec_and_get_output(["git", "diff", "--cached", "--name-only"]).splitlines()

    @staticmethod
    def git_unstaged_files():
        return exec_and_get_output(["git", "diff", "HEAD", "--name-only"]).splitlines()
