from abc import ABC, abstractmethod


class LlmProvider(ABC):

    def model_name(self):
        pass

