//! Versioned table schema storage.
//!
//! Two scopes exist:
//! * [`SchemaRegistry`] holds schema versions whose declaring transaction has
//!   committed; they remain available for the lifetime of the decoder so any
//!   still-open old-format transaction can keep decoding against them.
//! * per-transaction pending declarations keep a schema visible inside its own
//!   transaction before commit (DML and DDL can interleave inside one
//!   transaction) and allow an aborted DDL transaction to be discarded as a
//!   whole.

use std::collections::BTreeMap;

use crate::error::FeedError;
use crate::model::{SchemaVersion, TableId, TableSchema, TxnId};

#[derive(Debug, Default)]
pub struct SchemaRegistry {
    schemas: BTreeMap<(TableId, SchemaVersion), TableSchema>,
}

impl SchemaRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Look up an already committed schema version.
    pub fn get(&self, table_id: TableId, version: SchemaVersion) -> Option<&TableSchema> {
        self.schemas.get(&(table_id, version))
    }

    /// Whether the exact schema declaration is already committed.
    pub fn contains(&self, schema: &TableSchema) -> bool {
        self.schemas
            .get(&(schema.table_id, schema.version))
            .is_some_and(|existing| existing == schema)
    }

    /// Publish a schema declaration. Re-declaring the same table/version with
    /// identical contents is idempotent; different contents are an error.
    pub fn publish(&mut self, schema: TableSchema) -> Result<(), FeedError> {
        match self.schemas.get(&(schema.table_id, schema.version)) {
            Some(existing) if existing == &schema => Ok(()),
            Some(_) => Err(FeedError::ConflictingCommittedSchema {
                txn_id: 0,
                table_id: schema.table_id,
                schema_version: schema.version,
            }),
            None => {
                self.schemas
                    .insert((schema.table_id, schema.version), schema);
                Ok(())
            }
        }
    }

    /// Number of committed schema versions.
    pub fn len(&self) -> usize {
        self.schemas.len()
    }

    pub fn is_empty(&self) -> bool {
        self.schemas.is_empty()
    }
}

/// Schema declarations carried by an open transaction, not yet committed.
#[derive(Debug, Default)]
pub struct PendingSchemas {
    schemas: BTreeMap<(TableId, SchemaVersion), TableSchema>,
}

impl PendingSchemas {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record a schema declaration inside the owning transaction.
    ///
    /// Duplicate identical declarations are accepted; conflicting
    /// declarations for the same table/version are rejected, as are schemas
    /// whose column ids are not unique.
    pub fn add(
        &mut self,
        txn_id: TxnId,
        schema: TableSchema,
    ) -> Result<(), FeedError> {
        let mut seen = std::collections::BTreeSet::new();
        for column in &schema.columns {
            if !seen.insert(column.id) {
                return Err(FeedError::DuplicateColumnInSchema {
                    txn_id,
                    table_id: schema.table_id,
                    schema_version: schema.version,
                    column_id: column.id,
                });
            }
        }

        match self.schemas.get(&(schema.table_id, schema.version)) {
            Some(existing) if existing == &schema => Ok(()),
            Some(_) => Err(FeedError::ConflictingSchemaDeclaration {
                txn_id,
                table_id: schema.table_id,
                schema_version: schema.version,
            }),
            None => {
                self.schemas
                    .insert((schema.table_id, schema.version), schema);
                Ok(())
            }
        }
    }

    /// Resolve a version against the pending declarations only.
    pub fn get(&self, table_id: TableId, version: SchemaVersion) -> Option<&TableSchema> {
        self.schemas.get(&(table_id, version))
    }

    /// Move every pending declaration into the committed registry.
    pub fn publish_all(self, registry: &mut SchemaRegistry) -> Result<(), FeedError> {
        for (_, schema) in self.schemas {
            registry.publish(schema)?;
        }
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.schemas.len()
    }

    pub fn is_empty(&self) -> bool {
        self.schemas.is_empty()
    }
}
