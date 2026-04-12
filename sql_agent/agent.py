from asyncio import set_event_loop
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)



class Agent:

    def __init__(
            self,
            llm: LLM,
            prompt_templates: PromptTemplates,
            tools: Tools,
            skills: Skills,
            contexts: Contexts,
            max_steps: int = 20

    ):
        self.llm = llm
        self.prompt_templates = prompt_templates
        self.tools = tools
        self.skills = skills
        self.context = context
        self.max_steps = max_steps



    async def run(self, query: str):

