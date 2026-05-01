-- Set AHUJA OPTICAL CO as CHALLAN customer
-- =====================================================

-- Update AHUJA OPTICAL CO to use challan billing
UPDATE parties 
SET billing_preference = 'CHALLAN'
WHERE party_name ILIKE '%AHUJA OPTICAL CO%';

-- Verify the update
SELECT 
    id,
    party_name,
    mobile,
    billing_preference,
    created_at
FROM parties 
WHERE party_name ILIKE '%AHUJA OPTICAL CO%';

-- Also check if there are multiple similar parties
SELECT 
    COUNT(*) as total_similar_parties,
    STRING_AGG(party_name, ', ' ORDER BY party_name) as party_names
FROM parties 
WHERE party_name ILIKE '%AHUJA%' 
GROUP BY party_name ILIKE '%AHUJA OPTICAL CO%';
