$version: "2.0"

namespace ccf

/// Common sanitized wire members inherited by every status-specific error.
@mixin
structure ErrorResponse {
    @required
    code: ErrorCode

    /// Sanitized for callers. Internal details and configuration are never returned.
    @required
    message: SanitizedErrorMessage

    @required
    request_id: RequestId
}

@error("client")
@httpError(400)
structure BadRequestException with [ErrorResponse] {}

@error("client")
@httpError(401)
structure UnauthorizedException with [ErrorResponse] {}

/// Resource does not exist in the caller's account/Region partition.
@error("client")
@httpError(404)
structure ResourceNotFoundException with [ErrorResponse] {}

/// Conflicts with current state: connector IN_USE, job already terminal, or an
/// optimistic-lock version mismatch.
@error("client")
@httpError(409)
structure ConflictException with [ErrorResponse] {}

@error("client")
@httpError(429)
structure ThrottlingException with [ErrorResponse] {}

@error("server")
@httpError(500)
structure InternalServerError with [ErrorResponse] {}

@error("server")
@httpError(503)
structure ServiceUnavailableException with [ErrorResponse] {}
