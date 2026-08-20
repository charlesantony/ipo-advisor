from abc import ABC, abstractmethod

class IPOProvider(ABC):
    @abstractmethod
    def fetch_ipos(self, status="LIVE", ipo_type="MAINBOARD"):
        raise NotImplementedError
