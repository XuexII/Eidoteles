#!/usr/bin/env python
# coding=utf-8
# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import re
import shutil
from pathlib import Path
from typing import Generator

from smolagents.agent_types import AgentAudio, AgentImage, AgentText
from smolagents.agents import MultiStepAgent, PlanningStep
from memory import ActionStep, FinalAnswerStep
from smolagents.models import ChatMessageStreamDelta, MessageRole, agglomerate_stream_deltas
from smolagents.utils import _is_package_available
from sql_tools import ExecuteSQL, GenerateChart, ExecuteOutput, ChartOutput
import pandas as pd


def get_step_footnote_content(step_log: ActionStep | PlanningStep, step_name: str) -> str:
    """Get a footnote string for a step log with duration and token information"""
    step_footnote = f"**{step_name}**"
    if step_log.token_usage is not None:
        step_footnote += f" | Input tokens: {step_log.token_usage.input_tokens:,} | Output tokens: {step_log.token_usage.output_tokens:,}"
    step_footnote += f" | Duration: {round(float(step_log.timing.duration), 2)}s" if step_log.timing.duration else ""
    step_footnote_content = f"""<span style="color: #bbbbc2; font-size: 12px;">{step_footnote}</span> """
    return step_footnote_content


def _clean_model_output(model_output: str) -> str:
    """
    Clean up model output by removing trailing tags and extra backticks.

    Args:
        model_output (`str`): Raw model output.

    Returns:
        `str`: Cleaned model output.
    """
    if not model_output:
        return ""
    model_output = model_output.strip()
    # Remove any trailing <end_code> and extra backticks, handling multiple possible formats
    model_output = re.sub(r"```\s*<end_code>", "```", model_output)  # handles ```<end_code>
    model_output = re.sub(r"<end_code>\s*```", "```", model_output)  # handles <end_code>```
    model_output = re.sub(r"```\s*\n\s*<end_code>", "```", model_output)  # handles ```\n<end_code>
    return model_output.strip()


def _format_code_content(content: str) -> str:
    """
    Format code content as Python code block if it's not already formatted.

    Args:
        content (`str`): Code content to format.

    Returns:
        `str`: Code content formatted as a Python code block.
    """
    content = content.strip()
    # Remove existing code blocks and end_code tags
    content = re.sub(r"```.*?\n", "", content)
    content = re.sub(r"\s*<end_code>\s*", "", content)
    content = content.strip()
    # Add Python code block formatting if not already present
    if not content.startswith("```python"):
        content = f"```python\n{content}\n```"
    return content


def _process_action_step(step_log: ActionStep, skip_model_outputs: bool = False) -> Generator:
    """
    Process an [`ActionStep`] and yield appropriate Gradio ChatMessage objects.

    Args:
        step_log ([`ActionStep`]): ActionStep to process.
        skip_model_outputs (`bool`): Whether to skip model outputs.

    Yields:
        `gradio.ChatMessage`: Gradio ChatMessages representing the action step.
    """
    import gradio as gr

    sql = None
    df = None
    chart_fig = None
    # Output the step number
    step_number = f"Step {step_log.step_number}"
    if not skip_model_outputs:
        msg = gr.ChatMessage(role=MessageRole.ASSISTANT, content=f"**{step_number}**", metadata={"status": "done"})
        yield msg, sql, df, chart_fig


    # First yield the thought/reasoning from the LLM
    if not skip_model_outputs and getattr(step_log, "model_output", ""):
        model_output = _clean_model_output(step_log.model_output)
        msg = gr.ChatMessage(role=MessageRole.ASSISTANT, content=model_output, metadata={"status": "done"})
        yield msg, sql, df, chart_fig

    # For tool calls, create a parent message
    if getattr(step_log, "tool_calls", []):
        first_tool_call = step_log.tool_calls[0]
        fist_action_output = step_log.action_output[0]

        used_code = first_tool_call.name == "python_interpreter"

        # Process arguments based on type
        args = first_tool_call.arguments
        if isinstance(args, dict):
            content = str(args.get("answer", str(args)))
        else:
            content = str(args).strip()

        # Format code content if needed
        if used_code:
            content = _format_code_content(content)

        # 执行sql
        if first_tool_call.name == ExecuteSQL.name:
            sql = args.get("sql", content)
            if isinstance(fist_action_output, ExecuteOutput):
                content = "查询完成"
                df = pd.read_excel(fist_action_output.path)
        elif first_tool_call.name == GenerateChart.name:
            if isinstance(fist_action_output, ChartOutput):
                chart_fig = fist_action_output.fig

        # Create the tool call message
        parent_message_tool = gr.ChatMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            metadata={
                "title": f"🛠️ 调用工具 {first_tool_call.name}",
                "status": "done",
            },
        )
        msg = parent_message_tool

        yield msg, sql, df, chart_fig

    # Display execution logs if they exist
    if getattr(step_log, "observations", "") and step_log.observations.strip():
        log_content = step_log.observations.strip()
        if log_content:
            log_content = re.sub(r"^Execution logs:\s*", "", log_content)
            msg = gr.ChatMessage(
                role=MessageRole.ASSISTANT,
                content=f"```bash\n{log_content}\n```",
                metadata={"title": "📝 工具执行结果", "status": "done"},
            )

            yield msg, sql, df, chart_fig

    # Display any images in observations
    if getattr(step_log, "observations_images", []):
        for image in step_log.observations_images:
            path_image = AgentImage(image).to_string()
            msg = gr.ChatMessage(
                role=MessageRole.ASSISTANT,
                content={"path": path_image, "mime_type": f"image/{path_image.split('.')[-1]}"},
                metadata={"title": "🖼️ Output Image", "status": "done"},
            )
            yield msg, sql, df, chart_fig

    # Handle errors
    if getattr(step_log, "error", None):
        msg = gr.ChatMessage(
            role=MessageRole.ASSISTANT, content=str(step_log.error), metadata={"title": "💥 Error", "status": "done"}
        )
        yield msg, sql, df, chart_fig

    # Add step footnote and separator
    msg = gr.ChatMessage(
        role=MessageRole.ASSISTANT,
        content=get_step_footnote_content(step_log, step_number),
        metadata={"status": "done"},
    )

    yield msg, sql, df, chart_fig


def _process_planning_step(step_log: PlanningStep, skip_model_outputs: bool = False) -> Generator:
    """
    Process a [`PlanningStep`] and yield appropriate gradio.ChatMessage objects.

    Args:
        step_log ([`PlanningStep`]): PlanningStep to process.

    Yields:
        `gradio.ChatMessage`: Gradio ChatMessages representing the planning step.
    """
    import gradio as gr

    if not skip_model_outputs:
        yield gr.ChatMessage(role=MessageRole.ASSISTANT, content="**Planning step**", metadata={"status": "done"})
        yield gr.ChatMessage(role=MessageRole.ASSISTANT, content=step_log.plan, metadata={"status": "done"})
    yield gr.ChatMessage(
        role=MessageRole.ASSISTANT,
        content=get_step_footnote_content(step_log, "Planning step"),
        metadata={"status": "done"},
    )
    yield gr.ChatMessage(role=MessageRole.ASSISTANT, content="-----", metadata={"status": "done"})


def _process_final_answer_step(step_log: FinalAnswerStep) -> Generator:
    """
    Process a [`FinalAnswerStep`] and yield appropriate gradio.ChatMessage objects.

    Args:
        step_log ([`FinalAnswerStep`]): FinalAnswerStep to process.

    Yields:
        `gradio.ChatMessage`: Gradio ChatMessages representing the final answer.
    """
    import gradio as gr

    final_answer = step_log.output
    if isinstance(final_answer, AgentText):
        yield gr.ChatMessage(
            role=MessageRole.ASSISTANT,
            content=f"**分析报告:**\n{final_answer.to_string()}\n",
            metadata={"status": "done"},
        )
    elif isinstance(final_answer, AgentImage):
        yield gr.ChatMessage(
            role=MessageRole.ASSISTANT,
            content={"path": final_answer.to_string(), "mime_type": "image/png"},
            metadata={"status": "done"},
        )
    elif isinstance(final_answer, AgentAudio):
        yield gr.ChatMessage(
            role=MessageRole.ASSISTANT,
            content={"path": final_answer.to_string(), "mime_type": "audio/wav"},
            metadata={"status": "done"},
        )
    else:
        yield gr.ChatMessage(
            role=MessageRole.ASSISTANT, content=f"**Final answer:** {str(final_answer)}", metadata={"status": "done"}
        )


def pull_messages_from_step(step_log: ActionStep | PlanningStep | FinalAnswerStep, skip_model_outputs: bool = False):
    """Extract Gradio ChatMessage objects from agent steps with proper nesting.

    Args:
        step_log: The step log to display as gr.ChatMessage objects.
        skip_model_outputs: If True, skip the model outputs when creating the gr.ChatMessage objects:
            This is used for instance when streaming model outputs have already been displayed.
    """
    if not _is_package_available("gradio"):
        raise ModuleNotFoundError(
            "Please install 'gradio' extra to use the GradioUI: `pip install 'smolagents[gradio]'`"
        )
    if isinstance(step_log, ActionStep):
        yield from _process_action_step(step_log, skip_model_outputs)
    elif isinstance(step_log, PlanningStep):
        yield from _process_planning_step(step_log, skip_model_outputs)
    elif isinstance(step_log, FinalAnswerStep):
        yield from _process_final_answer_step(step_log)
    else:
        raise ValueError(f"Unsupported step type: {type(step_log)}")


def stream_to_gradio(
    agent,
    task: str,
    task_images: list | None = None,
    reset_agent_memory: bool = False,
    additional_args: dict | None = None,
) -> Generator:
    """Runs an agent with the given task and streams the messages from the agent as gradio ChatMessages."""

    if not _is_package_available("gradio"):
        raise ModuleNotFoundError(
            "Please install 'gradio' extra to use the GradioUI: `pip install 'smolagents[gradio]'`"
        )
    accumulated_events: list[ChatMessageStreamDelta] = []

    sql = None
    df = None
    chart_fig = None

    for event in agent.run(
        task, images=task_images, stream=True, reset=reset_agent_memory, additional_args=additional_args
    ):
        if isinstance(event, ActionStep | PlanningStep | FinalAnswerStep):
            for res in pull_messages_from_step(
                event,
                # If we're streaming model outputs, no need to display them twice
                skip_model_outputs=getattr(agent, "stream_outputs", False),
            ):
                print()
                if isinstance(event, ActionStep):
                    message, _sql, _df, _chart_fig = res
                    sql = sql or _sql
                    df = df or _df
                    chart_fig = chart_fig or _chart_fig
                else:
                    message = res

                yield message, sql, df, chart_fig
            accumulated_events = []

        elif isinstance(event, ChatMessageStreamDelta):
            accumulated_events.append(event)
            text = agglomerate_stream_deltas(accumulated_events).render_as_markdown()
            yield text


class GradioUI:
    """
    Gradio interface for interacting with a [`MultiStepAgent`].

    This class provides a web interface to interact with the agent in real-time, allowing users to submit prompts, upload files, and receive responses in a chat-like format.
    It uses the modern [`gradio.ChatInterface`] component for a native chatbot experience.
    It can reset the agent's memory at the start of each interaction if desired.
    It supports file uploads via multimodal input.
    This class requires the `gradio` extra to be installed: `pip install 'smolagents[gradio]'`.

    Args:
        agent ([`MultiStepAgent`]): The agent to interact with.
        file_upload_folder (`str`, *optional*): The folder where uploaded files will be saved.
            If not provided, file uploads are disabled.
        reset_agent_memory (`bool`, *optional*, defaults to `False`): Whether to reset the agent's memory at the start of each interaction.
            If `True`, the agent will not remember previous interactions.

    Raises:
        ModuleNotFoundError: If the `gradio` extra is not installed.

    Example:
        ```python
        from smolagents import CodeAgent, GradioUI, InferenceClientModel

        model = InferenceClientModel(model_id="meta-llama/Meta-Llama-3.1-8B-Instruct")
        agent = CodeAgent(tools=[], model=model)
        gradio_ui = GradioUI(agent, file_upload_folder="uploads", reset_agent_memory=True)
        gradio_ui.launch()
        ```
    """

    def __init__(self, agent: MultiStepAgent, file_upload_folder: str | None = None, reset_agent_memory: bool = False):
        if not _is_package_available("gradio"):
            raise ModuleNotFoundError(
                "Please install 'gradio' extra to use the GradioUI: `pip install 'smolagents[gradio]'`"
            )
        self.agent = agent
        self.file_upload_folder = Path(file_upload_folder) if file_upload_folder is not None else None
        self.reset_agent_memory = reset_agent_memory
        self.name = getattr(agent, "name") or "Agent interface"
        self.description = getattr(agent, "description", None)
        if self.file_upload_folder is not None:
            if not self.file_upload_folder.exists():
                self.file_upload_folder.mkdir(parents=True, exist_ok=True)

    def _save_uploaded_file(self, file_path: str) -> str:
        """Save an uploaded file to the upload folder and return the new path."""
        if self.file_upload_folder is None:
            return file_path

        original_name = os.path.basename(file_path)
        sanitized_name = re.sub(r"[^\w\-.]", "_", original_name)
        dest_path = os.path.join(self.file_upload_folder, sanitized_name)
        shutil.copy(file_path, dest_path)
        return dest_path

    def upload_file(self, file, file_uploads_log: list, allowed_file_types: list | None = None):
        """
        Handle file upload with validation.

        Args:
            file: The uploaded file object.
            file_uploads_log: List to track uploaded files.
            allowed_file_types: List of allowed extensions. Defaults to [".pdf", ".docx", ".txt"].

        Returns:
            Tuple of (status textbox, updated file log).
        """
        import gradio as gr

        if file is None:
            return gr.Textbox(value="No file uploaded", visible=True), file_uploads_log

        if allowed_file_types is None:
            allowed_file_types = [".pdf", ".docx", ".txt"]

        file_ext = os.path.splitext(file.name)[1].lower()
        if file_ext not in allowed_file_types:
            return gr.Textbox(value="File type disallowed", visible=True), file_uploads_log

        file_path = self._save_uploaded_file(file.name)
        return gr.Textbox(value=f"File uploaded: {file_path}", visible=True), file_uploads_log + [file_path]

    def _process_message(self, message: str | dict) -> tuple[str, list[str] | None]:
        """Process incoming message and extract text and files."""
        if isinstance(message, str):
            return message, None

        text = message.get("text", "")
        files = message.get("files", [])

        if files and self.file_upload_folder:
            saved_files = [self._save_uploaded_file(f) for f in files]
            if saved_files:
                text += f"\nYou have been provided with these files: {saved_files}"
            return text, saved_files

        return text, files if files else None

    def _stream_response(self, message: str | dict, history: list[dict]) -> Generator:  # noqa: ARG002
        """Stream agent responses for ChatInterface."""
        import gradio as gr

        task, task_files = self._process_message(message)

        all_messages: list[gr.ChatMessage] = []
        accumulated_events: list[ChatMessageStreamDelta] = []
        streaming_msg_idx: int | None = None

        sql = None
        df = None
        chart_fig = None

        for event in self.agent.run(
            task, images=task_files, stream=True, reset=self.reset_agent_memory, additional_args=None
        ):
            if isinstance(event, ActionStep | PlanningStep | FinalAnswerStep):
                # Remove streaming message if present
                if streaming_msg_idx is not None:
                    all_messages.pop(streaming_msg_idx)
                    streaming_msg_idx = None

                for res in pull_messages_from_step(
                    event,
                    skip_model_outputs=getattr(self.agent, "stream_outputs", False),
                ):
                    if isinstance(event, ActionStep):
                        msg, _sql, _df, _chart_fig = res
                        sql = sql or _sql
                        df = df or _df
                        chart_fig = chart_fig or _chart_fig
                    else:
                        msg = res

                    all_messages.append(
                        gr.ChatMessage(
                            role=msg.role,
                            content=msg.content,
                            metadata=msg.metadata,
                        )
                    )
                    yield all_messages, sql, df, chart_fig
                accumulated_events = []
            elif isinstance(event, ChatMessageStreamDelta):
                accumulated_events.append(event)
                text = agglomerate_stream_deltas(accumulated_events).render_as_markdown()
                text = text.replace("<", r"\<").replace(">", r"\>")
                msg = gr.ChatMessage(role="assistant", content=text)
                if streaming_msg_idx is None:
                    streaming_msg_idx = len(all_messages)
                    all_messages.append(msg)
                else:
                    all_messages[streaming_msg_idx] = msg
                yield all_messages

    def launch(self, share: bool = False, **kwargs):
        """
        Launch the Gradio app with the agent interface.

        Args:
            share (`bool`, defaults to `True`): Whether to share the app publicly.
            **kwargs: Additional keyword arguments to pass to the Gradio launch method.
        """
        self.create_app().launch(debug=True, share=share, **kwargs)

    def create_app(self):
        import gradio as gr

        type_messages_kwarg = {"type": "messages"} if gr.__version__.startswith("5") else {}

        with gr.Blocks(title="电商Text2SQL智能分析助手") as demo:
            gr.Markdown("# 电商数据分析助手")
            gr.Markdown("用文字输入需求，获取SQL查询、图表与分析报告。")

            with gr.Row():
                with gr.Column(scale=3):
                    # 关键修复：添加 type="tuples" 以使用元组格式
                    chatbot = gr.Chatbot(
                        label="对话历史",
                        height=550,
                        avatar_images=(None, None),  # 两个默认头像
                        latex_delimiters=[
                            {"left": r"$$", "right": r"$$", "display": True},
                            {"left": r"$", "right": r"$", "display": False},
                            {"left": r"\[", "right": r"\]", "display": True},
                            {"left": r"\(", "right": r"\)", "display": False},
                        ],
                        **type_messages_kwarg,
                    )
                    with gr.Row():
                        gr.Markdown(
                            "<div style='line-height: 38px; font-weight: bold;'>输入问题</div>",
                        )
                        query = gr.Textbox(placeholder="例如：上周销售额最高的5个商品", scale=5, show_label=False)
                        send_btn = gr.Button("发送", variant="primary", scale=5)

                with gr.Column(scale=2):
                    with gr.Accordion("查询的SQL", open=True):
                        sql_display = gr.Code(label="SQL语句", language="sql", interactive=False, max_lines=5)

                    with gr.Row("数据"):
                        data_table = gr.Dataframe(label="查询结果", interactive=False, max_height=120)

                    with gr.Row("图表"):
                        plot_display = gr.Plot(label="可视化结果", scale=3)

            send_event = send_btn.click(
                fn=self._stream_response,
                inputs=[query],
                outputs=[chatbot, sql_display, data_table, plot_display]
            ).then(
                fn=lambda: "",
                outputs=[query]
            )

            query.submit(
                fn=self._stream_response,
                inputs=[query],
                outputs=[chatbot, sql_display, data_table, plot_display]
            ).then(
                fn=lambda: "",
                outputs=[query]
            )

        return demo


__all__ = ["stream_to_gradio", "GradioUI"]
