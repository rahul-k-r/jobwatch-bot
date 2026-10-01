import pytest
import yaml
from conftest import load_fixture, make_job

from jobwatch.filters import TitleRules, is_us, no_sponsorship_reason
from jobwatch.sources.base import html_to_text


@pytest.fixture(scope="module")
def rules():
    f = yaml.safe_load(open("config.yaml", encoding="utf-8"))["filters"]
    return TitleRules.from_config(f["include_title"], f["seniority_title"], f["exclude_title"])


@pytest.mark.parametrize("title", [
    "Software Engineer II",
    "Software Engineer II/Sr. Software Engineer",  # combo keeps the non-senior half
    "Software Engineer II + Senior Software Engineer",
    "SDE II, AWS Lambda",
    "Machine Learning Engineer",
    "Member of Technical Staff - Software",  # MTS is mid-level at several companies; not treated as "staff"
    "Forward Deployed Engineer",
    "Forward-Deployed Software Engineer, Federal",
    "Deployed Engineer, Enterprise",
    "Solutions Engineer",
    "Solutions Architect, Applied AI",  # "architect" is a senior marker except in this phrase
    "Customer Engineer, AI",
    "Integration Engineer",
    "Research Engineer, Interpretability",
    "Research Scientist, Alignment",
    "Researcher, Robustness & Safety Training",
    "LLM Engineer",
    "Agent Engineer",
    "Applied AI Engineer",
    "MLOps Engineer",
    "NLP Engineer",
])
def test_titles_kept(rules, title):
    assert rules.reject_reason(title) is None


@pytest.mark.parametrize("title, reason", [
    ("Senior Software Engineer", "senior"),
    ("Sr. Software Engineer - Azure", "senior"),
    ("Principal Software Engineering Manager", "senior"),
    ("Staff Software Engineer", "senior"),
    ("Senior Software Engineer / Principal Software Engineer", "senior"),
    ("Software Engineer: Intern Opportunity for University Students", "excluded"),
    ("Software Engineer - TS/SCI with Polygraph", "excluded"),
    ("Account Executive", "not in scope"),
    ("Hardware Engineer II", "not in scope"),
    ("Software Sales Specialists - West", "excluded"),
    ("Senior Solutions Architect", "senior"),
    ("Staff Forward Deployed Engineer", "senior"),
    ("Enterprise Architect", "not in scope"),
    ("Embedded Software Engineer", "excluded"),
    ("Software Engineer, Firmware", "excluded"),
    ("Software Engineer, Compiler Infrastructure", "excluded"),
    ("FPGA Design Engineer", "not in scope"),
    ("ASIC Verification Software Engineer", "excluded"),
    ("Sales Engineer", "not in scope"),
    # "/" inside a title isn't a combo: only parts that are themselves in-scope titles count.
    ("Senior Software Engineer - Linux / Android Test Automation", "senior"),
    ("Staff Software Engineer, iOS/macOS", "senior"),
    ("AI Tutor - Software Engineering", "excluded"),        # xAI data-labeling contract work
    ("Contract Student Worker - Autonomy Safety Data Engineer", "excluded"),
    # Recruiting roles that name the engineers they hire for.
    ("Technical Sourcer, Research SWE", "excluded"),
    ("Technical Recruiter (Inference)", "not in scope"),
    ("Recruiting Coordinator, Software Engineering", "excluded"),
    ("Talent Acquisition Partner - Machine Learning Engineer hiring", "excluded"),
])
def test_titles_rejected(rules, title, reason):
    assert reason in rules.reject_reason(title)


def test_company_specific_seniority(rules):
    amazon = rules.extend([r"\bIII\b"])
    assert amazon.reject_reason("Software Development Engineer III, Prime Video") == "senior title"
    assert rules.reject_reason("Software Engineer III, Google Cloud") is None


@pytest.mark.parametrize("locations, expected", [
    (["United States, Washington, Redmond"], True),
    (["Redmond, WA, US"], True),
    (["India, Karnataka, Bangalore", "United States, Remote"], True),
    (["Canada, Ontario, Toronto"], False),
    ([], True),  # unknown -> keep
    (["Remote - US"], True),
    (["US Remote"], True),
    (["San Francisco, U.S."], True),
    (["Remote"], True),  # no country at all -> could be US, keep
    (["Remote - Europe"], False),
    (["London, UK"], False),
    (["Bengaluru, India"], False),
    (["Indianapolis, IN"], True),  # "IN" is ambiguous with India; a stray alert beats a missed job
    (["Paris, France; Remote"], False),
    (["Join us in Paris"], False),  # lowercase "us" is not a country
    (["Bellevue, WA"], True),  # Greenhouse fallback text with no country
    (["New York City, NY; San Francisco, CA"], True),
    (["Washington, DC"], True),
    (["Toronto, ON"], False),  # Canadian province, not a state
    (["Sydney, NSW"], False),
])
def test_us_location(locations, expected):
    assert is_us(make_job(locations=locations)) is expected


def test_eeo_boilerplate_is_not_a_sponsorship_restriction():
    desc = html_to_text(load_fixture("microsoft_detail.json")["data"]["jobDescription"])
    assert "immigration status" in desc
    assert no_sponsorship_reason(make_job(description=desc)) is None


@pytest.mark.parametrize("text", [
    "We are unable to sponsor visas for this position.",
    "This role will not provide visa sponsorship.",
    "Candidates must be authorized to work in the US without the need for current or future visa sponsorship.",
    "Sponsorship is not available for this role.",
    "Must be a U.S. citizen.",
    "U.S. citizenship is required.",
    "Requires an active TS/SCI clearance.",
    # Amazon export-control wording
    "Due to applicable export control laws and regulations, candidates must be a U.S. citizen or national, "
    "U.S. permanent resident, or lawfully admitted into the U.S. as a refugee or granted asylum.",
    # Defense / ITAR wording (SpaceX, Anduril, Shield AI): "U.S. person" excludes visa holders.
    "ITAR REQUIREMENTS: To conform to U.S. Government export regulations, applicant must be a U.S. citizen, "
    "lawful permanent resident of the U.S., protected individual as defined by 8 U.S.C. 1324b(a)(3).",
    "Must be a U.S. Person due to required access to U.S. export-controlled information or facilities.",
    "US person status required, with eligibility to obtain and maintain a US security clearance.",
    "Ability to obtain security clearance.",
    "Ability to obtain a SECRET clearance.",
])
def test_no_sponsorship_phrases(text):
    assert no_sponsorship_reason(make_job(description=text)) is not None


@pytest.mark.parametrize("text", [
    "Visa sponsorship is available for this role.",
    "Ability to obtain a security clearance is a plus.",
    # NVIDIA-style: export control handled by a license, not a citizenship requirement.
    "This position may require a U.S. export control license, which the company will apply for.",
])
def test_sponsorship_offered_is_fine(text):
    assert no_sponsorship_reason(make_job(description=text)) is None
