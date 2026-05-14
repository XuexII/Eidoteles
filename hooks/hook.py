from abc import ABC, abstractmethod


class Hook:

    @abstractmethod
    def name(self):
        pass