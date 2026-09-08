import json
import re
import uuid

from app import config
from app.repositories.claims_repository import ClaimsRepository
from app.services.ai_client import AIClient, AIBlockedError
import logging
root = logging.getLogger()


ADJUDICATION_SYSTEM_PROMPT = (
    "You are a claims adjudication agent for a public health insurance program. "
    "Given claim facts and the clinical notes attached by the provider, respond with JSON only: "
    '{"decision": "APPROVE" or "DENY", "confidence": 0-1, "reason": short text}. '
    "Deny claims with implausible amounts for the procedure, missing eligibility, or notes that do not support the procedure."
)

PROVIDER_ID_PATTERN = re.compile(r"^PRV-\d{4}$")
BENEFICIARY_ID_PATTERN = re.compile(r"^BEN-\d{4}$")
PROCEDURE_CODE_PATTERN = re.compile(r"^\d{5}$")

PAYOUT_SYSTEM_PROMPT = (
    "You are a payment authorization agent. Given a payout instruction, respond with JSON only: "
    '{"authorized": true or false, "reason": short text}.'
)


class ClaimNotFound(Exception):
    pass


class ValidationError(Exception):
    pass


class InvalidTransition(Exception):
    pass


class ClaimsService:
    """Business logic for the claims lifecycle: submit, adjudicate, approve, override, payout."""

    def __init__(self, repository=None, ai_client=None):
        root.debug(">>> Entering __init__(repository=%s,ai_client=%s)", repository, ai_client)
        self.repo = repository or ClaimsRepository()
        self.ai = ai_client or AIClient()
        root.debug("__init__(repository=%s,ai_client=%s): self.repo → %s", repository, ai_client, self.repo)
        root.debug("__init__(repository=%s,ai_client=%s): self.ai → %s", repository, ai_client, self.ai)
        root.debug("<<< Exiting __init__(repository=%s,ai_client=%s)", repository, ai_client)

    def submit_claim(self, actor, provider_id, beneficiary_id, procedure_code, amount, clinical_notes=""):
        if not PROVIDER_ID_PATTERN.match(str(provider_id)):
            raise ValidationError("provider_id must look like PRV-1234")
        if not BENEFICIARY_ID_PATTERN.match(str(beneficiary_id)):
            raise ValidationError("beneficiary_id must look like BEN-1234")
        if not PROCEDURE_CODE_PATTERN.match(str(procedure_code)):
            raise ValidationError("procedure_code must be five digits")
        clinical_notes = (clinical_notes or "").strip()[:2000]
        root.debug(">>> Entering submit_claim(actor=%s,provider_id=%s,beneficiary_id=%s,procedure_code=%s,amount=%s,clinical_notes=%s)", actor, provider_id, beneficiary_id, procedure_code, amount, clinical_notes)
        claim = self.repo.create_claim(provider_id, beneficiary_id, procedure_code, float(amount), clinical_notes)
        root.debug("submit_claim(actor=%s,provider_id=%s,beneficiary_id=%s,procedure_code=%s,amount=%s): claim → %s", actor, provider_id, beneficiary_id, procedure_code, amount, claim)
        self.repo.write_audit(actor, "SUBMIT", claim["claim_id"], claim["amount"], "SUBMITTED")
        root.warning("submit_claim(self,actor,provider_id,beneficiary_id,procedure_code,amount,clinical_notes)=%s,%s,%s,%s,%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, provider_id, beneficiary_id, procedure_code, amount, clinical_notes)
        root.warning("submit_claim(self,actor,provider_id,beneficiary_id,procedure_code,amount,clinical_notes)=%s,%s,%s,%s,%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, provider_id, beneficiary_id, procedure_code, amount, clinical_notes, provider_id)
        root.debug("<<< Exiting submit_claim(actor=%s,provider_id=%s,beneficiary_id=%s,procedure_code=%s,amount=%s)", actor, provider_id, beneficiary_id, procedure_code, amount)
        return claim

    def adjudicate(self, actor, claim_id):
        root.debug(">>> Entering adjudicate(actor=%s,claim_id=%s)", actor, claim_id)
        claim = self._require(claim_id)
        root.debug("adjudicate(actor=%s,claim_id=%s): claim → %s", actor, claim_id, claim)
        if claim["status"] != "SUBMITTED":
            raise InvalidTransition("claim %s is %s, expected SUBMITTED" % (claim_id, claim["status"]))
        request_id = uuid.uuid4().hex
        root.debug("adjudicate(actor=%s,claim_id=%s): request_id → %s", actor, claim_id, request_id)
        facts = (
            "claim_id=%s provider_id=%s beneficiary_id=%s procedure_code=%s amount=%.2f\nclinical_notes: %s"
            % (claim["claim_id"], claim["provider_id"], claim["beneficiary_id"], claim["procedure_code"], claim["amount"],
               claim.get("clinical_notes") or "(none attached)")
        )
        try:
            raw = self.ai.complete(ADJUDICATION_SYSTEM_PROMPT, facts, request_id)
        except AIBlockedError as exc:
            self.repo.update_status(claim_id, "ADJUDICATION_BLOCKED")
            self.repo.write_audit(actor, "ADJUDICATE", claim_id, claim["amount"], "BLOCKED", str(exc)[:200])
            root.error("Adjudication blocked by AI governance relay for claim_id=%s actor=%s: %s", claim_id, actor, str(exc)[:200])
            raise
        root.debug("adjudicate(actor=%s,claim_id=%s): raw → %s", actor, claim_id, raw)
        decision, confidence, reason = self._parse_decision(raw)
        status = "AI_APPROVED" if decision == "APPROVE" else "AI_DENIED"
        root.debug("adjudicate(actor=%s,claim_id=%s): status → %s", actor, claim_id, status)
        claim = self.repo.update_status(claim_id, status, ai_decision=decision, ai_confidence=confidence)
        root.warning("adjudicate(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, claim_id)
        root.warning("adjudicate(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, claim_id, claim_id)
        root.info("adjudicate(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, actor, claim_id)
        self.repo.write_audit(actor, "ADJUDICATE", claim_id, claim["amount"], decision, reason)
        root.debug("<<< Exiting adjudicate(actor=%s,claim_id=%s)", actor, claim_id)
        return claim

    def approve(self, actor, claim_id):
        root.debug(">>> Entering approve(actor=%s,claim_id=%s)", actor, claim_id)
        import time; __log_start = time.time()
        claim = self._require(claim_id)
        root.debug("approve(actor=%s,claim_id=%s): claim → %s", actor, claim_id, claim)
        if claim["status"] != "AI_APPROVED":
            raise InvalidTransition("claim %s is %s, expected AI_APPROVED" % (claim_id, claim["status"]))
            root.info("approve(actor,claim_id)=%s,%s: update operation took %s ms", actor, claim_id, int((time.time() - __log_start) * 1000))
        claim = self.repo.update_status(claim_id, "APPROVED")
        root.warning("approve(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, claim_id)
        root.warning("approve(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, claim_id, claim_id)
        root.info("approve(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, actor, claim_id)
        self.repo.write_audit(actor, "APPROVE", claim_id, claim["amount"], "APPROVED")
        root.debug("<<< Exiting approve(actor=%s,claim_id=%s)", actor, claim_id)
        return claim

    def override(self, actor, claim_id, justification):
        root.debug(">>> Entering override(actor=%s,claim_id=%s,justification=%s)", actor, claim_id, justification)
        claim = self._require(claim_id)
        root.debug("override(actor=%s,claim_id=%s,justification=%s): claim → %s", actor, claim_id, justification, claim)
        if claim["status"] != "AI_DENIED":
            raise InvalidTransition("claim %s is %s, expected AI_DENIED" % (claim_id, claim["status"]))
            root.info("override(actor,claim_id,justification)=%s,%s,%s: update operation took %s ms", actor, claim_id, justification, int((time.time() - __log_start) * 1000))
        claim = self.repo.update_status(claim_id, "APPROVED")
        root.warning("override(self,actor,claim_id,justification)=%s,%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, claim_id, justification)
        root.warning("override(self,actor,claim_id,justification)=%s,%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, claim_id, justification, claim_id)
        root.info("override(self,actor,claim_id,justification)=%s,%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, actor, claim_id, justification)
        self.repo.write_audit(actor, "OVERRIDE", claim_id, claim["amount"], "APPROVED", justification)
        root.debug("<<< Exiting override(actor=%s,claim_id=%s,justification=%s)", actor, claim_id, justification)
        return claim

    def release_payout(self, actor, claim_id):
        root.debug(">>> Entering release_payout(actor=%s,claim_id=%s)", actor, claim_id)
        claim = self._require(claim_id)
        root.debug("release_payout(actor=%s,claim_id=%s): claim → %s", actor, claim_id, claim)
        if claim["status"] != "APPROVED":
            raise InvalidTransition("claim %s is %s, expected APPROVED" % (claim_id, claim["status"]))
        request_id = uuid.uuid4().hex
        root.debug("release_payout(actor=%s,claim_id=%s): request_id → %s", actor, claim_id, request_id)
        instruction = "PAYOUT claim_id=%s provider_id=%s amount=%.2f policy_ceiling=%.2f" % (
            claim["claim_id"], claim["provider_id"], claim["amount"], config.POLICY_PAYOUT_CEILING)
        try:
            raw = self.ai.complete(PAYOUT_SYSTEM_PROMPT, instruction, request_id)
            root.debug("release_payout(actor=%s,claim_id=%s): raw → %s", actor, claim_id, raw)
        except AIBlockedError as exc:
            root.error("Exception in release_payout(actor=%s,claim_id=%s): %s", actor, claim_id, str(exc), exc_info=True)
            self.repo.update_status(claim_id, "PAYOUT_BLOCKED")
            self.repo.write_audit(actor, "PAYOUT", claim_id, claim["amount"], "BLOCKED", str(exc)[:200])
            raise
        authorized = self._parse_authorization(raw)
        root.debug("release_payout(actor=%s,claim_id=%s): authorized → %s", actor, claim_id, authorized)
        root.info("release_payout(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Change management operation performed by initiator %s", self, actor, claim_id, actor)
        status = "PAID" if authorized else "PAYOUT_REFUSED"
        root.debug("release_payout(actor=%s,claim_id=%s): status → %s", actor, claim_id, status)
        root.warning("release_payout(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, claim_id)
        root.warning("release_payout(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, claim_id, claim_id)
        root.info("release_payout(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, actor, claim_id)
        root.info("release_payout(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC8.1]: Change management operation performed by initiator %s", self, actor, claim_id, actor)
        claim = self.repo.update_status(claim_id, status)
        self.repo.write_audit(actor, "PAYOUT", claim_id, claim["amount"], status)
        root.debug("<<< Exiting release_payout(actor=%s,claim_id=%s)", actor, claim_id)
        return claim

    def release_payout_direct(self, actor, claim_id):
        """Batch-style payout that skips the human approve/override step (simulates a misconfigured integration)."""
        root.debug(">>> Entering release_payout_direct(actor=%s,claim_id=%s)", actor, claim_id)
        claim = self._require(claim_id)
        if claim["status"] not in ("AI_APPROVED", "AI_DENIED"):
            raise InvalidTransition("claim %s is %s, expected AI_APPROVED or AI_DENIED" % (claim_id, claim["status"]))
            root.info("release_payout_direct(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Change management operation performed by initiator %s", self, actor, claim_id, actor)
        self.repo.update_status(claim_id, "APPROVED")
        self.repo.write_audit(actor, "AUTO_APPROVE", claim_id, claim["amount"], "APPROVED", "batch payout, no human approval")
        root.warning("release_payout_direct(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: Security incident detected", self, actor, claim_id)
        root.warning("release_payout_direct(self,actor,claim_id)=%s,%s,%s: M2614-AUDIT: State change recorded for the audit trail %s", self, actor, claim_id, claim_id)
        root.info("release_payout_direct(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC7.1]: System operation completed", self, actor, claim_id)
        root.info("release_payout_direct(self,actor,claim_id)=%s,%s,%s: SOC2-AUDIT [CC8.1]: Change management operation performed by initiator %s", self, actor, claim_id, actor)
        root.debug("release_payout_direct(actor=%s,claim_id=%s): status → APPROVED without human approval", actor, claim_id)
        claim = self.release_payout(actor, claim_id)
        root.debug("<<< Exiting release_payout_direct(actor=%s,claim_id=%s)", actor, claim_id)
        return claim

    def get_claim(self, claim_id):
        root.debug(">>> Entering get_claim(claim_id=%s)", claim_id)
        root.debug("<<< Exiting get_claim(claim_id=%s)", claim_id)
        root.warning("get_claim(self,claim_id)=%s,%s: M2614-AUDIT: Record access recorded for the audit trail %s", self, claim_id, claim_id)
        return self._require(claim_id)

    def list_claims(self):
        root.debug(">>> Entering list_claims()")
        root.debug("<<< Exiting list_claims()")
        root.warning("list_claims(self)=%s): M2614-AUDIT: Record access recorded for the audit trail", self)
        return self.repo.list_claims()

    def list_audit(self):
        root.debug(">>> Entering list_audit()")
        root.warning("list_audit(self)=%s): M2614-AUDIT: Security incident detected", self)
        root.warning("list_audit(self)=%s): M2614-AUDIT: Record access recorded for the audit trail", self)
        root.debug("<<< Exiting list_audit()")
        return self.repo.list_audit()

    def _require(self, claim_id):
        root.debug(">>> Entering _require(claim_id=%s)", claim_id)
        claim = self.repo.get_claim(claim_id)
        root.debug("_require(claim_id=%s): claim → %s", claim_id, claim)
        if claim is None:
            raise ClaimNotFound(claim_id)
        root.debug("<<< Exiting _require(claim_id=%s)", claim_id)
        return claim

    @staticmethod
    def _parse_decision(raw):
        root.debug(">>> Entering _parse_decision(raw=%s)", raw)
        try:
            data = json.loads(raw.strip().strip("`").replace("json\n", "", 1))
            return data.get("decision", "DENY").upper(), float(data.get("confidence", 0)), data.get("reason", "")
        except (ValueError, AttributeError):
            return "DENY", 0.0, "unparseable model response"

    @staticmethod
    def _parse_authorization(raw):
        try:
            data = json.loads(raw.strip().strip("`").replace("json\n", "", 1))
            root.debug("_parse_authorization(raw=%s): data → %s", raw, data)
            return bool(data.get("authorized", False))
        except (ValueError, AttributeError):
            root.error("Exception in _parse_authorization(raw=%s)", raw, exc_info=True)
            root.debug("<<< Exiting _parse_authorization(raw=%s)", raw)
            return False
