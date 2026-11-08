$version: "2.0"

namespace ccf

/// Members every error response carries.
@mixin
structure ErrorResponse {
    @required
    code: ErrorCode

    /// Safe to show the caller: it holds no internal detail or configuration value.
    @required
    message: SanitizedErrorMessage

    @required
    request_id: RequestId
}

@error("client")
@httpError(400)
structure BadRequestException with [ErrorResponse] {}

/// The caller isn't allowed to call the operation, or its account isn't an allowed tenant account.
@error("client")
@httpError(403)
structure UnauthorizedException with [ErrorResponse] {}

/// A resource in the path doesn't exist for the caller's tenant and parent.
@error("client")
@httpError(404)
structure ResourceNotFoundException with [ErrorResponse] {}

/// The request conflicts with the resource's status, a running job, an earlier
/// request with the same client token, or a collection that still holds documents.
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
