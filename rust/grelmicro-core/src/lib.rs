//! Compiled hot paths for grelmicro.
//!
//! Only work that earns the crossing lives here. A call into Rust costs 55
//! nanoseconds measured, so anything doing less than about a microsecond of
//! real work belongs in Python, which is where the token cache and the ban
//! table stayed after being measured both ways.
//!
//! What is here today is JWT verification, where a signature check is some
//! twelve microseconds and the crossing is rounding error. The Python layer
//! owns configuration, error taxonomy and framework wiring. This crate owns
//! key selection, base64 decode, signature verification, registered claim
//! checks and claim materialisation, all in one call across the boundary.
//!
//! Verification releases the GIL for the work that touches no Python object.

use std::collections::{HashMap, HashSet};

use aws_lc_rs::digest;
use jsonwebtoken::jwk::Jwk;
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use serde_json::Value;

create_exception!(
    grelmicro_core,
    CoreVerificationError,
    PyException,
    "Raised when a token fails verification, carrying (reason, detail)."
);

/// One key as the Python layer hands it over: `(kid, algorithm, material, format)`.
///
/// `format` is `pem` for PEM or a raw `HS*` secret, and `jwk` for a single JWK
/// serialised as JSON. A JWKS is passed as one entry per key it holds.
type KeySpec = Vec<(Option<String>, String, Vec<u8>, String)>;

/// Map a `jsonwebtoken` failure to the stable tag the Python layer branches on.
fn reason_of(error: &jsonwebtoken::errors::Error) -> &'static str {
    use jsonwebtoken::errors::ErrorKind;
    match error.kind() {
        ErrorKind::InvalidToken | ErrorKind::Base64(_) | ErrorKind::Json(_) => "malformed",
        ErrorKind::InvalidSignature => "signature",
        ErrorKind::ExpiredSignature => "expired",
        ErrorKind::ImmatureSignature => "not-yet-valid",
        ErrorKind::InvalidAudience => "audience",
        ErrorKind::InvalidIssuer => "issuer",
        ErrorKind::InvalidAlgorithm | ErrorKind::InvalidAlgorithmName => "algorithm",
        ErrorKind::MissingRequiredClaim(_) => "missing-claim",
        _ => "invalid",
    }
}

/// Build the exception the Python layer converts into `TokenRejectedError`.
///
/// The token itself is never part of the message: it is a live credential and
/// the message reaches logs.
fn rejected(reason: &str, detail: String) -> PyErr {
    CoreVerificationError::new_err((reason.to_string(), detail))
}

fn algorithm_of(name: &str) -> PyResult<Algorithm> {
    match name {
        "HS256" => Ok(Algorithm::HS256),
        "HS384" => Ok(Algorithm::HS384),
        "HS512" => Ok(Algorithm::HS512),
        "RS256" => Ok(Algorithm::RS256),
        "RS384" => Ok(Algorithm::RS384),
        "RS512" => Ok(Algorithm::RS512),
        "PS256" => Ok(Algorithm::PS256),
        "PS384" => Ok(Algorithm::PS384),
        "PS512" => Ok(Algorithm::PS512),
        "ES256" => Ok(Algorithm::ES256),
        "ES384" => Ok(Algorithm::ES384),
        "EdDSA" => Ok(Algorithm::EdDSA),
        other => Err(PyValueError::new_err(format!(
            "unsupported algorithm: {other}"
        ))),
    }
}

fn decoding_key(algorithm: Algorithm, key: &[u8], format: &str) -> PyResult<DecodingKey> {
    if format == "jwk" {
        let jwk: Jwk = serde_json::from_slice(key)
            .map_err(|error| PyValueError::new_err(format!("invalid JWK: {error}")))?;
        return DecodingKey::from_jwk(&jwk)
            .map_err(|error| PyValueError::new_err(format!("invalid JWK: {error}")));
    }
    let built = match algorithm {
        Algorithm::HS256 | Algorithm::HS384 | Algorithm::HS512 => {
            return Ok(DecodingKey::from_secret(key))
        }
        Algorithm::RS256
        | Algorithm::RS384
        | Algorithm::RS512
        | Algorithm::PS256
        | Algorithm::PS384
        | Algorithm::PS512 => DecodingKey::from_rsa_pem(key),
        Algorithm::ES256 | Algorithm::ES384 => DecodingKey::from_ec_pem(key),
        Algorithm::EdDSA => DecodingKey::from_ed_pem(key),
        other => {
            return Err(PyValueError::new_err(format!(
                "unsupported algorithm: {other:?}"
            )))
        }
    };
    built.map_err(|error| PyValueError::new_err(format!("invalid key: {error}")))
}

/// Convert parsed claims into Python objects.
fn value_to_py(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(flag) => flag.into_pyobject(py)?.to_owned().unbind().into_any(),
        Value::Number(number) => {
            if let Some(signed) = number.as_i64() {
                signed.into_pyobject(py)?.unbind().into_any()
            } else if let Some(unsigned) = number.as_u64() {
                unsigned.into_pyobject(py)?.unbind().into_any()
            } else {
                number
                    .as_f64()
                    .unwrap_or(f64::NAN)
                    .into_pyobject(py)?
                    .unbind()
                    .into_any()
            }
        }
        Value::String(text) => text.into_pyobject(py)?.unbind().into_any(),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(value_to_py(py, item)?)?;
            }
            list.unbind().into_any()
        }
        Value::Object(fields) => {
            let dict = PyDict::new(py);
            for (name, item) in fields {
                dict.set_item(name.as_str(), value_to_py(py, item)?)?;
            }
            dict.unbind().into_any()
        }
    })
}

/// Registered claims `jsonwebtoken` checks itself, given `required_spec_claims`.
///
/// Every other required claim is checked here, because the crate skips the
/// names it does not recognise rather than failing on them.
const SPEC_CLAIMS: [&str; 5] = ["aud", "exp", "iss", "nbf", "sub"];

/// Media type prefix RFC 7515 lets a `typ` header leave out.
const APPLICATION: &str = "application/";

/// Types a token may declare when no type is required.
///
/// `JWT` and `JOSE` are what issuers write when they type nothing in
/// particular, and `at+jwt` is the access token type of RFC 9068. Anything
/// else names another kind of token, such as a proof of possession or a logout token,
/// which is signed by the same keys and must never pass as an access token.
const ACCESS_TYPES: [&str; 3] = ["jwt", "jose", "at+jwt"];

/// Return a `typ` value without the `application/` prefix RFC 7515 allows.
///
/// Matched without regard to case, because a media type is compared that way.
fn without_application(declared: &str) -> &str {
    match (
        declared.get(..APPLICATION.len()),
        declared.get(APPLICATION.len()..),
    ) {
        (Some(prefix), Some(rest)) if prefix.eq_ignore_ascii_case(APPLICATION) => rest,
        _ => declared,
    }
}

/// Check the `typ` header against the type this verifier requires, if any.
///
/// With no type required, a token that declares none passes, and so does one
/// declaring an access token type. With one required, the token must declare
/// exactly that type.
fn type_accepted(declared: Option<&str>, required: Option<&str>) -> bool {
    match (declared.map(without_application), required) {
        (None, required) => required.is_none(),
        (Some(found), None) => ACCESS_TYPES
            .iter()
            .any(|name| found.eq_ignore_ascii_case(name)),
        (Some(found), Some(expected)) => found.eq_ignore_ascii_case(expected),
    }
}

/// A verifier holding one key per `kid` plus an optional default key.
#[pyclass(frozen, module = "grelmicro_core")]
pub struct Verifier {
    keys: HashMap<String, (DecodingKey, Validation)>,
    fallback: Option<(DecodingKey, Validation)>,
    extra_required: Vec<String>,
    audience: Option<HashSet<String>>,
    issuer: Option<HashSet<String>>,
    token_type: Option<String>,
}

/// Check the `aud` claim, which RFC 7519 allows to be a string or an array.
///
/// `jsonwebtoken` reads this through a permissive parse and treats a value it
/// could not parse as absent, so a token carrying `aud: 1` or `aud: [1, 2]`
/// passes an audience check it never matched. A claim of the wrong shape is
/// refused here rather than ignored.
///
/// A claim that is absent still passes. An AWS Cognito access token carries no
/// `aud` at all, and `required` is what insists on one.
///
/// `accepted` being `None` means the service answers to no audience, which
/// RFC 7519 says must refuse a token that carries one. That is checked here
/// rather than left to the crate, because the crate reads a wrongly typed
/// `aud` as absent and would let `aud: 1` through where it refuses
/// `aud: "other"`.
fn audience_matches(claim: Option<&Value>, accepted: Option<&HashSet<String>>) -> bool {
    let matches = |name: &str| accepted.is_some_and(|names| names.contains(name));
    match claim {
        None | Some(Value::Null) => true,
        Some(Value::String(value)) => matches(value.as_str()),
        Some(Value::Array(values)) => {
            !values.is_empty()
                && values.iter().all(|value| matches!(value, Value::String(_)))
                && values
                    .iter()
                    .any(|value| value.as_str().is_some_and(&matches))
        }
        Some(_) => false,
    }
}

/// Check the `iss` claim, which RFC 7519 defines as a single `StringOrURI`.
///
/// An array is not a valid issuer, so a token carrying one is refused rather
/// than matched against its members. A wrongly typed `iss` is refused whether
/// or not an issuer is configured, for the same reason the audience is.
fn issuer_matches(claim: Option<&Value>, accepted: Option<&HashSet<String>>) -> bool {
    match claim {
        None | Some(Value::Null) => true,
        Some(Value::String(value)) => accepted.is_none_or(|names| names.contains(value.as_str())),
        Some(_) => false,
    }
}

/// Message for a token that names no key and finds no default.
fn no_kid() -> String {
    "token carries no kid".to_string()
}

impl Verifier {
    /// Pick the key the token's `kid` names, or the key for tokens without one.
    ///
    /// The declared type is checked first, so a token of another kind is
    /// refused before it can mark the key set stale by naming an unknown key.
    fn select(&self, token: &str) -> Result<&(DecodingKey, Validation), PyErr> {
        let header =
            decode_header(token).map_err(|error| rejected(reason_of(&error), error.to_string()))?;
        if !type_accepted(header.typ.as_deref(), self.token_type.as_deref()) {
            return Err(rejected("type", "token type not accepted".to_string()));
        }
        match header.kid {
            Some(kid) => self
                .keys
                .get(&kid)
                .ok_or_else(|| rejected("unknown-key", format!("no key for kid {kid}"))),
            // With no `kid` the default key answers, and a lone registered
            // key answers for it when there is no default.
            None => match &self.fallback {
                Some(found) => Ok(found),
                None if self.keys.len() == 1 => self
                    .keys
                    .values()
                    .next()
                    .ok_or_else(|| rejected("unknown-key", no_kid())),
                None => Err(rejected("unknown-key", no_kid())),
            },
        }
    }

    fn claims_of(&self, token: &str) -> Result<Value, PyErr> {
        let (key, validation) = self.select(token)?;
        let claims = decode::<Value>(token, key, validation)
            .map(|data| data.claims)
            .map_err(|error| rejected(reason_of(&error), error.to_string()))?;
        // A token carrying `cnf` (RFC 7800) is bound to a key, and is only
        // good together with proof that the caller holds that key. Nothing
        // here checks such a proof, so accepting the token would drop the
        // binding its issuer asked for. Null counts as absent, as it does
        // for every required claim.
        if matches!(claims.get("cnf"), Some(value) if !value.is_null()) {
            return Err(rejected("binding", "token is bound to a key".to_string()));
        }
        for name in &self.extra_required {
            // A claim written as `null` is absent, not present with no
            // value. The registered claims are read this way by the crate,
            // so reading these any other way would make `required` a weaker
            // promise for the claims a caller adds than for the ones the
            // RFC names.
            if !matches!(claims.get(name), Some(value) if !value.is_null()) {
                return Err(rejected(
                    "missing-claim",
                    format!("missing required claim {name}"),
                ));
            }
        }
        // Run unconditionally. A token carrying a wrongly typed `aud` or
        // `iss` must be refused whether or not a policy names one, because
        // the crate reads such a claim as absent.
        if !audience_matches(claims.get("aud"), self.audience.as_ref()) {
            return Err(rejected("audience", "InvalidAudience".to_string()));
        }
        if !issuer_matches(claims.get("iss"), self.issuer.as_ref()) {
            return Err(rejected("issuer", "InvalidIssuer".to_string()));
        }
        Ok(claims)
    }
}

#[pymethods]
impl Verifier {
    /// Build a verifier from `(kid, algorithm, key)` triples and a claim policy.
    ///
    /// A `kid` of `None` registers the key used for tokens with no `kid`
    /// header. PEM material is parsed here, never per verification.
    /// `token_type` requires every token to declare that type, such as
    /// `at+jwt`, instead of accepting any access token type.
    #[new]
    #[pyo3(signature = (keys, *, audience=None, issuer=None, leeway=0, required=None, token_type=None))]
    fn new(
        keys: KeySpec,
        audience: Option<Vec<String>>,
        issuer: Option<Vec<String>>,
        leeway: u64,
        required: Option<Vec<String>>,
        token_type: Option<String>,
    ) -> PyResult<Self> {
        if keys.is_empty() {
            return Err(PyValueError::new_err("at least one key is required"));
        }
        let token_type = match token_type {
            Some(named) if without_application(&named).is_empty() => {
                return Err(PyValueError::new_err("token_type must name a type"));
            }
            Some(named) => Some(without_application(&named).to_string()),
            None => None,
        };
        let required: HashSet<String> = required
            .unwrap_or_else(|| vec!["exp".to_string()])
            .into_iter()
            .collect();

        let extra_required: Vec<String> = required
            .iter()
            .filter(|name| !SPEC_CLAIMS.contains(&name.as_str()))
            .cloned()
            .collect();

        let audience = audience.unwrap_or_default();
        let issuer = issuer.unwrap_or_default();
        let accepted_audience: Option<HashSet<String>> =
            (!audience.is_empty()).then(|| audience.iter().cloned().collect());
        let accepted_issuer: Option<HashSet<String>> =
            (!issuer.is_empty()).then(|| issuer.iter().cloned().collect());

        let mut registered = HashMap::with_capacity(keys.len());
        let mut fallback = None;
        for (kid, name, material, format) in keys {
            let algorithm = algorithm_of(&name)?;
            let key = decoding_key(algorithm, &material, &format)?;
            let mut validation = Validation::new(algorithm);
            validation.leeway = leeway;
            // The crate leaves `nbf` unchecked by default. A token that says
            // it is not valid yet has to be refused.
            validation.validate_nbf = true;
            validation.required_spec_claims.clone_from(&required);
            if !audience.is_empty() {
                validation.set_audience(&audience);
            }
            if !issuer.is_empty() {
                validation.set_issuer(&issuer);
            }
            match kid {
                Some(kid) => {
                    registered.insert(kid, (key, validation));
                }
                None => fallback = Some((key, validation)),
            }
        }
        Ok(Self {
            keys: registered,
            fallback,
            extra_required,
            audience: accepted_audience,
            issuer: accepted_issuer,
            token_type,
        })
    }

    /// Verify `token` and return its claims as a dict.
    ///
    /// Raises `CoreVerificationError(reason, detail)` for a token that fails
    /// any check.
    fn verify(&self, py: Python<'_>, token: &str) -> PyResult<Py<PyAny>> {
        let claims = py.detach(|| self.claims_of(token))?;
        value_to_py(py, &claims)
    }
}

/// Return the raw SHA-256 digest of `token`.
///
/// Used as a cache key, so a verified token is remembered without the token
/// itself being held in memory. The digest is returned as 32 raw bytes rather
/// than as hex: hex doubles the key and costs more to format than the hash
/// costs to compute.
#[pyfunction]
fn sha256_digest<'py>(py: Python<'py>, token: &str) -> Bound<'py, PyBytes> {
    let computed = digest::digest(&digest::SHA256, token.as_bytes());
    PyBytes::new(py, computed.as_ref())
}

/// Return the `alg` and `kid` of `token` without verifying its signature.
#[pyfunction]
fn unverified_header(py: Python<'_>, token: &str) -> PyResult<Py<PyAny>> {
    let header =
        decode_header(token).map_err(|error| rejected(reason_of(&error), error.to_string()))?;
    let dict = PyDict::new(py);
    dict.set_item("alg", format!("{:?}", header.alg))?;
    dict.set_item("kid", header.kid)?;
    Ok(dict.unbind().into_any())
}

// Declared free-threading safe rather than left to the binding default. A
// module that does not declare this makes CPython turn the GIL back on when
// it is imported, which would silently cost every other extension in the
// process its parallelism. Nothing here needs the GIL: `Verifier` is frozen,
// holds only what construction put in it, and is never mutated afterwards,
// which the `frozen` attribute makes the compiler enforce.
#[pymodule(gil_used = false)]
fn grelmicro_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Verifier>()?;
    module.add_function(wrap_pyfunction!(unverified_header, module)?)?;
    module.add_function(wrap_pyfunction!(sha256_digest, module)?)?;
    module.add(
        "CoreVerificationError",
        module.py().get_type::<CoreVerificationError>(),
    )?;
    Ok(())
}
