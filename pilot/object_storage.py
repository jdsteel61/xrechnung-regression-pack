"""Short-lived raw fixture storage for the single-order staging access model."""
import re
import uuid


_ID = re.compile(r"[a-f0-9]{32}\Z")


class ObjectScopeError(ValueError):
    pass


class S3RawFixtures:
    """Application-scoped S3 access. The bucket must be private and dedicated.

    An order is the current tenant boundary. Keys are generated here, never
    accepted from HTTP callers. This does not substitute for per-job IAM.
    """
    def __init__(self, client, bucket, prefix="raw/"):
        if not bucket or not re.fullmatch(r"[a-zA-Z0-9._-]+", bucket):
            raise ValueError("invalid_bucket")
        if prefix != "raw/":
            raise ValueError("invalid_raw_prefix")
        self.client, self.bucket, self.prefix = client, bucket, prefix

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ObjectScopeError("invalid_object_scope")
        return value

    def key(self, tenant, job, index, side):
        self._id(tenant)
        self._id(job)
        if type(index) is not int or not 0 <= index < 250 or side not in ("old", "new"):
            raise ObjectScopeError("invalid_object_scope")
        return f"{self.prefix}{tenant}/{job}/{index:04d}-{side}.xml"

    def put(self, tenant, job, index, side, data):
        key = self.key(tenant, job, index, side)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType="application/xml")
        return key

    def get(self, tenant, job, index, side):
        key = self.key(tenant, job, index, side)
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete_job(self, tenant, job):
        prefix = f"{self.prefix}{self._id(tenant)}/{self._id(job)}/"
        self._delete_prefix(prefix)

    def sweep(self):
        """Startup recovery for the dedicated bucket's raw namespace."""
        self._delete_prefix(self.prefix)

    def list_raw(self, max_pages=10):
        """Bounded listing of the whole raw namespace for verification evidence.

        Raises rather than returning a partial list: an unconfirmable listing
        must never be mistaken for an empty namespace.
        """
        keys, token = [], None
        for _ in range(max_pages):
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix,
                MaxKeys=1000, **({'ContinuationToken': token} if token else {}))
            keys.extend(row["Key"] for row in page.get("Contents", []))
            if any(not key.startswith(self.prefix) for key in keys):
                raise ObjectScopeError("object_listing_out_of_scope")
            if not page.get("IsTruncated"):
                return keys
            token = page.get("NextContinuationToken")
            if not token:
                raise OSError("object_listing_incomplete")
        raise OSError("object_listing_unconfirmed")

    def _delete_prefix(self, prefix):
        for _ in range(100):
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=1000)
            keys = [row["Key"] for row in page.get("Contents", [])]
            # Refuse malformed provider results; never delete outside scope.
            if any(not key.startswith(prefix) for key in keys):
                raise ObjectScopeError("object_listing_out_of_scope")
            if keys:
                result = self.client.delete_objects(Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True})
                if result.get("Errors"):
                    raise OSError("object_deletion_failed")
            if not keys:
                if page.get("IsTruncated"):
                    raise OSError("object_listing_incomplete")
                return
        raise OSError("object_deletion_unconfirmed")

    @staticmethod
    def new_job():
        return uuid.uuid4().hex
