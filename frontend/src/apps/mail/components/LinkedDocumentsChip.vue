<template>
	<div>
		<!-- Header row, same rhythm as the attachments header. Shows only when the message
		     carries links; the count comes from the cheap v1 endpoint — the drawer carries
		     the rich view (previews, unlink), fetched when it opens. -->
		<button
			v-if="documents.data?.length"
			class="text-ink-gray-5 hover:text-ink-gray-8 flex items-center gap-1.5 text-sm"
			@click="emit('open')"
		>
			<Link2 class="h-3.5 w-3.5" />
			<span>{{ __('{0} linked documents', [String(documents.data.length)]) }}</span>
			<Badge
				v-for="doc in documents.data.slice(0, 2)"
				:key="`${doc.doctype}/${doc.name}`"
				:label="doc.doctype_label"
				variant="subtle"
				class="ml-1"
			/>
			<span v-if="documents.data.length > 2" class="text-ink-gray-4">
				+{{ documents.data.length - 2 }}
			</span>
		</button>
	</div>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import { Link2 } from 'lucide-vue-next'
import { Badge, createResource } from 'frappe-ui'

import type { Mail } from '@/apps/mail/types'

const { mail } = defineProps<{ mail: Mail }>()
const emit = defineEmits<{ open: [] }>()

const documents = createResource({
	url: 'suite.mail.api.link.get_message_linked_documents',
	makeParams: () => ({ name: mail.name }),
})

watch(
	() => mail.name,
	() => (documents.data?.length ? documents.reload() : documents.fetch()),
	{ immediate: true },
)
</script>
