# Life Model

The state below is the model's observation of the user — inferred, not declared.
SOUL.md and USER.md carry the assistant's constraints and the user's own
statements; those always override anything inferred here.

{% if life_goals %}
## Life Goals

{{ life_goals }}
{% endif %}

{% if current_state %}
## Current State

{{ current_state }}
{% endif %}

{% if patterns %}
## Confirmed Patterns

{{ patterns }}
{% endif %}

{% if expression %}
## Communication Preferences

{{ expression }}
{% endif %}
