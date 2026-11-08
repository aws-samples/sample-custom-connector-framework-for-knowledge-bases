---
description: The errors every Custom Connector Framework API operation can return, and when.
---

# Errors

Every error response carries `code`, `message`, and `request_id`. The message holds no internal detail and no configuration value. When the framework can't assume or use one of your roles, the message names that role's ARN so you can fix its policy. Quote the `request_id` when you report a problem to your administrator.

| Exception | HTTP | `code` | When |
| --- | --- | --- | --- |
| `BadRequestException` | 400 | `BAD_REQUEST` | A request member fails validation. The API's log names the member, and the response doesn't. |
| `UnauthorizedException` | 403 | `UNAUTHORIZED` | The [caller table](index.md#callers) doesn't allow the caller, the caller's account isn't allowed, or the framework can't assume or use a role the request depends on. Every caller is already authenticated by Signature Version 4, so the API answers 403, not 401. |
| `ResourceNotFoundException` | 404 | `RESOURCE_NOT_FOUND` | A resource in the path doesn't exist or belongs to another tenant or parent, or no checkpoint is stored. |
| `ConflictException` | 409 | `CONFLICT` | A status check failed, a `client_token` was reused with another request, or a collection still has documents. |
| `ThrottlingException` | 429 | `THROTTLED` | The API, or a service behind it, throttled the call. Retry with backoff. |
| `ServiceUnavailableException` | 503 | `SERVICE_UNAVAILABLE` | A service behind the API failed. |
| `InternalServerError` | 500 | `INTERNAL_SERVER_ERROR` | Anything else. |

The toolkit's client retries `ThrottlingException`, and retries `ServiceUnavailableException` and `InternalServerError` for operations that are safe to repeat.
