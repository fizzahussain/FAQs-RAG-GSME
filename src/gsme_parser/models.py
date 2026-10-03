from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class ParseWarning:
    code: str
    message: str
    location: str | None = None


@dataclass(slots=True)
class FAQRecord:
    faq_id: str
    category: str
    question: str
    answer: str
    source_file: str
    source_kind: str
    source_location: str
    faq_number: str | None = None
    page_start: int | None = None
    page_end: int | None = None


@dataclass(slots=True)
class ScheduleRecord:
    schedule_type: str
    technology_nm: int
    flavor: str
    trial_deadline: date
    final_deadline: date
    tapeout_date: date
    estimated_ship_date: date
    source_file: str
    source_row: int

    @property
    def schedule_id(self) -> str:
        flavor = "-".join(self.flavor.lower().replace("+", " plus ").split())
        return (
            f"{self.schedule_type}-{self.technology_nm}-{flavor}-"
            f"{self.tapeout_date.isoformat()}"
        )


@dataclass(slots=True)
class ParseResult:
    faq_records: list[FAQRecord] = field(default_factory=list)
    schedule_records: list[ScheduleRecord] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)

