from aicodebot.agents import SidekickAgent
from aicodebot.coder import Coder
from aicodebot.config import Session
from aicodebot.input import Chat, SidekickCompleter
from aicodebot.lm import DEFAULT_CONTEXT_TOKENS, DEFAULT_MEMORY_TOKENS, LanguageModelManager, get_token_size
from aicodebot.output import OurMarkdown, RichLiveCallbackHandler, get_console
from aicodebot.prompts import generate_files_context, get_prompt
from pathlib import Path
from prompt_toolkit import prompt as input_prompt
from prompt_toolkit.history import FileHistory
from rich.live import Live
import click, sys


@click.command
@click.option("-r", "--request", help="What to ask your sidekick to do")
@click.option("-n", "--no-files", is_flag=True, help="Don't automatically load any files for context")
@click.option("-m", "--max-file-tokens", type=int, default=10_000, help="Don't load files larger than this")
@click.argument("files", nargs=-1, type=click.Path(exists=True, readable=True))
def sidekick(request, no_files, max_file_tokens, files):  # noqa: PLR0915
    """
    Coding help from your AI sidekick
    FILES: List of files to be used as context for the session
    """
    console = get_console()
    if not Coder.is_inside_git_repo():
        console.print("🛑 This command must be run from within a git repository.", style=console.error_style)
        sys.exit(1)

    console.print("This is an experimental feature. We love bug reports 😉", style=console.warning_style)

    # ----------------- Determine which files to use for context ----------------- #

    if files:  # User supplied list of files
        context = generate_files_context(files)
    elif not no_files:
        # Determine which files to use for context automagically, with git
        session_data = Session.read()
        if session_data.get("files"):
            console.print("Using files from the last session for context.")
            files = session_data["files"]
        else:
            console.print("Using recent git commits and current changes for context.")
            files = Coder.auto_file_context(DEFAULT_CONTEXT_TOKENS, max_file_tokens)

        context = generate_files_context(files)
    else:
        context = generate_files_context([])

    # Convert it from a list or a tuple to a set to remove duplicates
    files = set(files)

    # ----------------------------- Set up langchain ----------------------------- #

    lmm = LanguageModelManager()
    # Generate the prompt and set up the model
    prompt = get_prompt("sidekick")

    def calculate_request_token_size():
        return get_token_size(prompt.template) + get_token_size(context) + DEFAULT_MEMORY_TOKENS

    request_token_size = calculate_request_token_size()

    model_name = lmm.choose_model(request_token_size)
    langchain_model = lmm.get_langchain_model(model_name, streaming=True)
    memory = lmm.get_memory(langchain_model)
    chain = lmm.get_langchain_chain(langchain_model, prompt, memory)

    # ---------------------- Set up the chat loop and prompt --------------------- #
    chat = Chat(console, files)
    chat.show_file_context()
    languages = ",".join(Coder.identify_languages(files))

    console.print(
        "Enter a request for your AICodeBot sidekick. Type / to see available commands.\n",
        style=console.bot_style,
    )
    history_file = Path.home() / ".aicodebot_request_history"
    completer = SidekickCompleter()
    completer.files = files

    current_model_name = model_name
    while True:  # continuous loop for multiple questions
        if request:
            human_input = request
        else:
            human_input = input_prompt("🤖 ➤ ", history=FileHistory(history_file), completer=completer)

        parsed_human_input = chat.parse_human_input(human_input)
        if parsed_human_input == chat.BREAK:
            break

        # Update the context for the new list of files
        context = generate_files_context(chat.files)
        languages = ",".join(Coder.identify_languages(chat.files))
        if completer.files != chat.files:
            completer.files = chat.files
            session_data = Session.read()
            session_data["files"] = list(chat.files)
            Session.write(session_data)

        if parsed_human_input == chat.CONTINUE:
            continue

        # If we got this far, it's a string that we are going to pass to the LLM

        # --------------- Process the input and stream it to the human --------------- #
        if parsed_human_input != human_input:
            # If the user edited the input, then we want to print it out so they
            # have a record of what they asked for on their terminal
            console.print(parsed_human_input)

        # Reset up the model for each question because the request token size may change it
        request_token_size = calculate_request_token_size()
        model_name = lmm.choose_model(request_token_size)
        if model_name != current_model_name:
            console.print(
                f"Using model {model_name} for a token request size of {request_token_size}", style="bold green"
            )
            model_name = lmm.choose_model(request_token_size)
            langchain_model = lmm.get_langchain_model(model_name, streaming=True)
            chain = lmm.get_langchain_chain(langchain_model, prompt, memory)
            current_model_name = model_name

        try:
            with Live(OurMarkdown(""), auto_refresh=True) as live:
                callback = RichLiveCallbackHandler(live, console.bot_style)
                langchain_model.callbacks = [callback]  # a fresh callback handler for each question

                chain.run({"task": parsed_human_input, "context": context, "languages": languages})

        except KeyboardInterrupt:
            console.print("\n\nOk, I'll stop talking. Hit Ctrl-C again to quit.", style=console.bot_style)
            continue

        if request:
            # If we were given a request, then we only want to run once
            break


@click.command
@click.option("-l", "--learned-repos", multiple=True, help="The name of the repo to use for learned information")
def sidekick_agent(learned_repos):
    """
    EXPERIMENTAL: Coding help from your AI sidekick, made agentic with tools
    """
    console = get_console()
    if not Coder.is_inside_git_repo():
        console.print("🛑 This command must be run from within a git repository.", style=console.error_style)
        sys.exit(1)

    console.print("This is an experimental feature.", style=console.warning_style)

    agent = SidekickAgent.get_agent_executor(learned_repos)
    history_file = Path.home() / ".aicodebot_request_history"

    console.print("Enter a request for your AICodeBot sidekick", style=console.bot_style)

    edited_input = None
    while True:  # continuous loop for multiple questions
        human_input = input_prompt("🤖 ➤ ", history=FileHistory(history_file))
        human_input = human_input.strip()

        if not human_input:
            # Must have been spaces or blank line
            continue

        elif human_input.lower()[-2:] == r"\e":
            # If the text ends wit then we want to edit it
            human_input = edited_input = click.edit(human_input[:-2])

        if edited_input:
            # If the user edited the input, then we want to print it out so they
            # have a record of what they asked for on their terminal
            console.print(f"Request:\n{edited_input}")

        response = agent.run(human_input)
        # Remove everything after Action: (if it exists)
        response = response.split("Action:")[0]
        console.print(OurMarkdown(response))