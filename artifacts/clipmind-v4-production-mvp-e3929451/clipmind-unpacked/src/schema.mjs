const REQUIRED_FIELDS = ['summary', 'keyIdeas', 'actionItems', 'importantFacts', 'suggestedTags'];

function toCleanString(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function toStringList(value) {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean);
}

export function validateModelDraft(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return { valid: false, errors: ['Draft must be an object.'] };
  }

  if ('userNotes' in input) {
    return { valid: false, errors: ['Model output must not include userNotes.'] };
  }

  const normalized = {
    summary: toCleanString(input.summary),
    keyIdeas: toStringList(input.keyIdeas),
    actionItems: toStringList(input.actionItems),
    importantFacts: toStringList(input.importantFacts),
    suggestedTags: toStringList(input.suggestedTags)
  };

  const errors = [];

  for (const field of REQUIRED_FIELDS) {
    if (!(field in input)) {
      errors.push(`Missing field: ${field}`);
    }
  }

  if (!normalized.summary) {
    errors.push('Summary is required.');
  }

  for (const listField of REQUIRED_FIELDS.slice(1)) {
    if (!Array.isArray(input[listField])) {
      errors.push(`${listField} must be an array of strings.`);
    }
  }

  return {
    valid: errors.length === 0,
    errors,
    value: normalized
  };
}

export function validateDraft(input) {
  const result = validateModelDraft(input);
  if (!result.valid) {
    const error = new Error(result.errors.join(' '));
    error.code = 'invalid-response';
    throw error;
  }
  return result.value;
}

export function sanitizeProviderDraft(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) {
    return input;
  }

  const clone = { ...input };
  delete clone.userNotes;
  return clone;
}

export function sanitizeUserTags(tags) {
  return [...new Set(toStringList(tags))];
}

export function normalizeDraftInput(input = {}) {
  return {
    ...validateDraft({
      summary: input.summary ?? input.draft?.summary ?? '',
      keyIdeas: input.keyIdeas ?? input.draft?.keyIdeas ?? [],
      actionItems: input.actionItems ?? input.draft?.actionItems ?? [],
      importantFacts: input.importantFacts ?? input.draft?.importantFacts ?? [],
      suggestedTags: input.suggestedTags ?? input.draft?.suggestedTags ?? []
    }),
    userNotes: typeof input.userNotes === 'string' ? input.userNotes : ''
  };
}

export function createEmptyDraft() {
  return {
    summary: '',
    keyIdeas: [],
    actionItems: [],
    importantFacts: [],
    suggestedTags: [],
    userNotes: ''
  };
}
