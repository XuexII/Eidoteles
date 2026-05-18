from copy import deepcopy
from typing import Any

from smolagents import OpenAIModel, ChatMessage, Tool, MessageRole
from smolagents.models import get_tool_json_schema, tool_role_conversions, REMOVE_PARAMETER
from smolagents.utils import encode_image_base64, make_image_url


def get_clean_message_list(
        message_list: list[ChatMessage | dict],
        role_conversions: dict[MessageRole, MessageRole] | dict[str, str] = {},
        convert_images_to_image_urls: bool = False,
        flatten_messages_as_text: bool = False,
) -> list[dict[str, Any]]:
    """
    Creates a list of messages to give as input to the LLM. These messages are dictionaries and chat template compatible with transformers LLM chat template.
    Subsequent messages with the same role will be concatenated to a single message.

    Args:
        message_list (`list[ChatMessage | dict]`): List of chat messages. Mixed types are allowed.
        role_conversions (`dict[MessageRole, MessageRole]`, *optional* ): Mapping to convert roles.
        convert_images_to_image_urls (`bool`, default `False`): Whether to convert images to image URLs.
        flatten_messages_as_text (`bool`, default `False`): Whether to flatten messages as text.
    """
    output_message_list: list[dict[str, Any]] = []
    message_list = deepcopy(message_list)  # Avoid modifying the original list
    for message in message_list:
        if isinstance(message, dict):
            message = ChatMessage.from_dict(message)
        role = message.role
        if role not in MessageRole.roles():
            raise ValueError(f"Incorrect role {role}, only {MessageRole.roles()} are supported for now.")

        if role in role_conversions:
            message.role = role_conversions[role]  # type: ignore
        # encode images if needed
        if isinstance(message.content, list):
            for element in message.content:
                assert isinstance(element, dict), "Error: this element should be a dict:" + str(element)
                if element["type"] == "image":
                    assert not flatten_messages_as_text, f"Cannot use images with {flatten_messages_as_text=}"
                    if convert_images_to_image_urls:
                        element.update(
                            {
                                "type": "image_url",
                                "image_url": {"url": make_image_url(encode_image_base64(element.pop("image")))},
                            }
                        )
                    else:
                        element["image"] = encode_image_base64(element["image"])

        if len(output_message_list) > 0 and message.role == output_message_list[-1]["role"]:
            assert isinstance(message.content, list), "Error: wrong content:" + str(message.content)
            if flatten_messages_as_text:
                output_message_list[-1]["content"] += "\n" + message.content[0]["text"]
            else:
                for el in message.content:
                    if el["type"] == "text" and output_message_list[-1]["content"][-1]["type"] == "text":
                        # Merge consecutive text messages rather than creating new ones
                        output_message_list[-1]["content"][-1]["text"] += "\n" + el["text"]
                    else:
                        output_message_list[-1]["content"].append(el)
        else:
            if flatten_messages_as_text:
                content = message.content[0]["text"]
            else:
                content = message.content
            output_message_list.append(
                {
                    "role": message.role,
                    "content": content,
                    "tool_calls": message.tool_calls
                }
            )
    return output_message_list


class SQLModel(OpenAIModel):
    def _prepare_completion_kwargs(
            self,
            messages: list[ChatMessage | dict],
            stop_sequences: list[str] | None = None,
            response_format: dict[str, str] | None = None,
            tools_to_call_from: list[Tool] | None = None,
            custom_role_conversions: dict[str, str] | None = None,
            convert_images_to_image_urls: bool = False,
            tool_choice: str | dict | None = "required",  # Configurable tool_choice parameter
            **kwargs,
    ) -> dict[str, Any]:
        """
        Prepare parameters required for model invocation.

        Parameter priority (highest to lowest):
        1. self.kwargs (model defaults)
        2. Explicitly passed kwargs
        3. Specific parameters (stop_sequences, response_format, etc.)
        """
        # Clean and standardize the message list
        flatten_messages_as_text = kwargs.pop("flatten_messages_as_text", self.flatten_messages_as_text)
        messages_as_dicts = get_clean_message_list(
            messages,
            role_conversions=custom_role_conversions or tool_role_conversions,
            convert_images_to_image_urls=convert_images_to_image_urls,
            flatten_messages_as_text=flatten_messages_as_text,
        )
        # Start with messages
        completion_kwargs = {
            "messages": messages_as_dicts,
        }
        # Override with specific parameters
        if stop_sequences is not None and self.supports_stop_parameter:
            # Some models do not support stop parameter
            completion_kwargs["stop"] = stop_sequences
        if response_format is not None:
            completion_kwargs["response_format"] = response_format
        if tools_to_call_from:
            completion_kwargs["tools"] = [get_tool_json_schema(tool) for tool in tools_to_call_from]
            if tool_choice is not None:
                completion_kwargs["tool_choice"] = tool_choice
        # Override with passed-in kwargs
        completion_kwargs.update(kwargs)
        # Override with self.kwargs
        for kwarg_name, kwarg_value in self.kwargs.items():
            if kwarg_value is REMOVE_PARAMETER:
                completion_kwargs.pop(kwarg_name, None)  # Remove parameter if present
            else:
                completion_kwargs[kwarg_name] = kwarg_value  # Set/override parameter
        return completion_kwargs
