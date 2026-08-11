"""Google Contacts (People API) lookup — read only."""

from __future__ import annotations

from ..registry import obj, tool
from .google_auth import service

GROUP = "contacts"

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations"


def _svc():
    return service("people", "v1")


def _clean(person: dict) -> dict:
    names = person.get("names") or []
    orgs = person.get("organizations") or []
    return {
        "name": names[0].get("displayName") if names else None,
        "emails": [e.get("value") for e in person.get("emailAddresses", [])],
        "phones": [p.get("value") for p in person.get("phoneNumbers", [])],
        "organization": orgs[0].get("name") if orgs else None,
        "title": orgs[0].get("title") if orgs else None,
    }


@tool(
    group=GROUP,
    name="contacts_search",
    description=(
        "Look up a person in the user's Google Contacts by name, email, or "
        "phone. Use this to turn a name the user mentions ('email Sarah about "
        "the invoice') into an actual address before sending anything. Searches "
        "both saved contacts and 'other contacts' (people they have emailed "
        "before but not saved)."
    ),
    schema=obj(
        {
            "query": {
                "type": "string",
                "description": "Name, email fragment, or phone number to search for.",
            },
            "max_results": {
                "type": "integer",
                "description": "How many matches to return (1-30). Default 10.",
            },
        },
        required=["query"],
    ),
)
def contacts_search(query: str, max_results: int = 10) -> dict:
    max_results = max(1, min(int(max_results), 30))
    svc = _svc()
    people: list[dict] = []

    saved = (
        svc.people()
        .searchContacts(query=query, pageSize=max_results, readMask=PERSON_FIELDS)
        .execute()
    )
    for result in saved.get("results", []):
        people.append({**_clean(result.get("person", {})), "source": "contacts"})

    if len(people) < max_results:
        other = (
            svc.otherContacts()
            .search(
                query=query,
                pageSize=max_results - len(people),
                readMask="names,emailAddresses",
            )
            .execute()
        )
        for result in other.get("results", []):
            people.append({**_clean(result.get("person", {})), "source": "other_contacts"})

    return {"query": query, "count": len(people), "people": people}


@tool(
    group=GROUP,
    name="contacts_list",
    description="List saved Google Contacts. Use contacts_search when looking for someone specific.",
    schema=obj(
        {
            "max_results": {
                "type": "integer",
                "description": "How many contacts to return (1-200). Default 50.",
            }
        }
    ),
)
def contacts_list(max_results: int = 50) -> dict:
    max_results = max(1, min(int(max_results), 200))
    connections = (
        _svc()
        .people()
        .connections()
        .list(
            resourceName="people/me",
            pageSize=max_results,
            personFields=PERSON_FIELDS,
            sortOrder="FIRST_NAME_ASCENDING",
        )
        .execute()
        .get("connections", [])
    )
    return {
        "count": len(connections),
        "people": [_clean(p) for p in connections],
    }
